"""
Whole-account problems, as opposed to one message failing.

Some Meta errors are about the company's WhatsApp account, not the message: billing
(131042 — the error that stopped a campaign on 13 Nov 2026), the account being locked
(131031), a policy block (368). Every send after the first fails the same way, so a
5,000-person broadcast would fail 5,000 times and an agent would keep pressing Send
into it.

The breaker (`check`, run every minute):
  * a campaign whose recent failures include ≥ TRIP_AFTER of one account-level code and
    nothing delivered → the account is marked blocked (`blocked_reason`, `blocked_at`)
    and an error-log entry explains what to fix in Meta.
  * ACCOUNT_BREAKER_MODE=enforce additionally pauses the campaign (status "paused" —
    the dispatcher only claims "sending" campaigns) and makes agent sends answer
    503 account_unavailable.
  * the block clears itself when a message for that company is delivered again.

Webhook fields other than `messages` (handled by `handle_field`):
  * message_template_status_update — a template APPROVED / PAUSED / DISABLED /
    REINSTATED / REJECTED at Meta updates our copy at once, so the agent app's list and
    the send-time check are never hours behind.
  * phone_number_quality_update — the number's messaging tier and quality event.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

ACCOUNT_LEVEL_CODES = {
    "131042": "Meta cannot bill this WhatsApp account (payment method). Fix it in Meta "
              "Business Settings → Billing, then resend.",
    "131031": "Meta has locked this WhatsApp account. Check WhatsApp Manager for the reason.",
    "368":    "Meta has temporarily blocked this account for policy violations.",
}
TRIP_AFTER = 5
LOOKBACK = timedelta(hours=6)


def enforcing() -> bool:
    return (settings.ACCOUNT_BREAKER_MODE or "alert").lower() == "enforce"


def block_for(db: Session, company_id) -> str | None:
    """The reason new sends are refused for this company, or None. Only in enforce mode."""
    if not enforcing():
        return None
    acc = db.query(WhatsAppAccount).filter(WhatsAppAccount.company_id == company_id).first()
    return acc.blocked_reason if acc and acc.blocked_at else None


def status_for(db: Session, company_id) -> dict:
    """For GET /limits: what the app should know about the account right now."""
    acc = db.query(WhatsAppAccount).filter(WhatsAppAccount.company_id == company_id).first()
    if acc is None:
        return {"state": "not_connected", "reason": None, "messaging_limit_tier": None}
    state = "ok"
    if acc.blocked_at:
        state = "blocked" if enforcing() else "warning"
    return {"state": state, "reason": acc.blocked_reason,
            "messaging_limit_tier": acc.messaging_limit_tier}


def check(db: Session, now: datetime | None = None) -> dict:
    """Trip or clear the breaker for every company with a recent campaign."""
    now = now or datetime.now(timezone.utc)
    tripped = cleared = 0
    rows = db.execute(text("""
        SELECT c.id AS campaign_id, c.company_id, c.status, r.error_code,
               COUNT(*) FILTER (WHERE r.status = 'failed') AS failed,
               COUNT(*) FILTER (WHERE r.status IN ('delivered', 'read')) AS delivered
          FROM broadcast_campaigns c
          JOIN broadcast_recipients r ON r.campaign_id = c.id
         WHERE c.status IN ('sending', 'dispatched')
           AND COALESCE(c.dispatched_at, c.created_at) > :since
           AND (r.error_code = ANY(:codes) OR r.status IN ('delivered', 'read'))
         GROUP BY c.id, c.company_id, c.status, r.error_code
    """), {"since": now - LOOKBACK, "codes": list(ACCOUNT_LEVEL_CODES)}).fetchall()

    by_campaign: dict = {}
    for r in rows:
        agg = by_campaign.setdefault(r.campaign_id, {"company_id": r.company_id,
                                                     "status": r.status,
                                                     "delivered": 0, "codes": {}})
        agg["delivered"] += r.delivered or 0
        if r.error_code in ACCOUNT_LEVEL_CODES:
            agg["codes"][r.error_code] = agg["codes"].get(r.error_code, 0) + (r.failed or 0)

    for campaign_id, agg in by_campaign.items():
        if agg["delivered"]:
            continue
        code, n = max(agg["codes"].items(), key=lambda kv: kv[1], default=(None, 0))
        if n >= TRIP_AFTER:
            if _trip(db, agg["company_id"], campaign_id, code, now):
                tripped += 1

    cleared = _clear_recovered(db)
    db.commit()
    return {"tripped": tripped, "cleared": cleared}


def _trip(db: Session, company_id, campaign_id, code: str, now: datetime) -> bool:
    acc = db.query(WhatsAppAccount).filter(WhatsAppAccount.company_id == company_id).first()
    if acc is None:
        return False
    newly = acc.blocked_at is None
    if newly:
        acc.blocked_reason = f"{code}: {ACCOUNT_LEVEL_CODES[code]}"
        acc.blocked_at = now
        log_error("WhatsApp account problem — sends are failing",
                  "account_health.check",
                  Exception(acc.blocked_reason),
                  request_data={"company_id": str(company_id),
                                "campaign_id": str(campaign_id),
                                "mode": settings.ACCOUNT_BREAKER_MODE})
    if enforcing():
        db.execute(text("""UPDATE broadcast_campaigns SET status = 'paused'
                            WHERE id = :c AND status = 'sending'"""), {"c": str(campaign_id)})
    return newly


def _clear_recovered(db: Session) -> int:
    """A delivery after the block was set means the account works again."""
    n = db.execute(text("""
        UPDATE whatsapp_accounts w SET blocked_reason = NULL, blocked_at = NULL
         WHERE w.blocked_at IS NOT NULL
           AND (EXISTS (SELECT 1 FROM broadcast_recipients r
                          JOIN broadcast_campaigns c ON c.id = r.campaign_id
                         WHERE c.company_id = w.company_id
                           AND r.delivered_at > w.blocked_at)
             OR EXISTS (SELECT 1 FROM agent_messages m
                          JOIN agent_threads t ON t.id = m.thread_id
                         WHERE t.company_id = w.company_id
                           AND m.delivered_at > w.blocked_at))
    """)).rowcount
    return n or 0


# ── Webhook fields other than `messages` ──────────────────────────────────────

_TEMPLATE_EVENT_TO_STATUS = {"REINSTATED": "APPROVED", "FLAGGED": None}


def handle_field(db: Session, field: str, value: dict, waba_id: str | None) -> bool:
    """Returns True if the field was handled."""
    if field == "message_template_status_update":
        return _template_status(db, value, waba_id)
    if field == "phone_number_quality_update":
        return _quality(db, value, waba_id)
    return False


def _template_status(db: Session, value: dict, waba_id: str | None) -> bool:
    event = str(value.get("event") or "").upper()
    if not event:
        return False
    status = _TEMPLATE_EVENT_TO_STATUS.get(event, event)
    if status is None:
        return True                      # FLAGGED: a quality warning, still sendable
    q = db.query(WhatsAppTemplate)
    tid = value.get("message_template_id")
    if tid is not None:
        q = q.filter(WhatsAppTemplate.meta_template_id == str(tid))
    else:
        q = q.filter(WhatsAppTemplate.name == value.get("message_template_name"),
                     WhatsAppTemplate.language == value.get("message_template_language"))
        if waba_id:
            q = q.filter(WhatsAppTemplate.waba_id == str(waba_id))
    changed = 0
    for tpl in q.all():
        tpl.status = status
        if status == "REJECTED" and value.get("reason"):
            tpl.rejection_reason = str(value["reason"])[:500]
        changed += 1
    db.commit()
    logger.info("Template status webhook: %s → %s (%d row(s))",
                value.get("message_template_name") or tid, status, changed)
    return True


def _quality(db: Session, value: dict, waba_id: str | None) -> bool:
    if not waba_id:
        return False
    accounts = db.query(WhatsAppAccount).filter(WhatsAppAccount.waba_id == str(waba_id)).all()
    for acc in accounts:
        if value.get("current_limit"):
            acc.messaging_limit_tier = str(value["current_limit"])[:40]
        if value.get("event"):
            acc.quality_event = str(value["event"])[:40]
    db.commit()
    return True
