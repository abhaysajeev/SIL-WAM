"""
Delivery receipts for broadcast recipients.

Broadcast writes no Message rows (docs/BROADCAST_DESIGN.md §8), so
`conversation_engine.handle_status` cannot find its target the usual way — it resolves
by `Message.wamid` and returns when there is no match. Without the fallback below,
**every broadcast receipt would be silently discarded**: no delivered or read counts,
and no invalid-number detection at all.

`handle()` is called only from that already-dead `if not msg:` branch. A transactional
message always has a Message row, so it never reaches here — which is what keeps the
live SFA/Shirin pipeline byte-for-byte unchanged.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.broadcast_campaign import BroadcastCampaign, BroadcastRecipient
from app.models.messaging import InvalidNumber

logger = logging.getLogger(__name__)

# Meta's "not a WhatsApp user". Routinely arrives seconds *after* a send Meta already
# accepted, which is why it is handled here rather than at send time.
INVALID_NUMBER_CODES = {"131026", 131026}

# Receipts arrive out of order — a "read" can land before its "delivered". Ranking
# prevents a late lower-grade receipt from walking a recipient backwards.
_RANK = {"sent": 1, "delivered": 2, "read": 3}


def handle(db: Session, wamid: str, state: str, status: dict) -> bool:
    """
    Apply a status receipt to a broadcast recipient.

    Returns True if this wamid belonged to a broadcast (so the caller stops), False if
    it is unknown here and the caller should carry on with its own handling.

    Does not commit — the caller owns the transaction.
    """
    if not wamid:
        return False

    recipient = (
        db.query(BroadcastRecipient)
        .filter(BroadcastRecipient.wamid == wamid)
        .first()
    )
    if recipient is None:
        return False

    campaign = (
        db.query(BroadcastCampaign)
        .filter(BroadcastCampaign.id == recipient.campaign_id)
        .first()
    )

    ts = _timestamp(status)

    if state == "failed":
        _apply_failure(db, recipient, campaign, status)
    elif state in _RANK:
        _apply_progress(recipient, campaign, state, ts)
    else:
        logger.debug("Broadcast receipt with unhandled state=%s wamid=%s", state, wamid)

    return True


def _timestamp(status: dict) -> datetime:
    raw = status.get("timestamp")
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc) if raw else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _apply_progress(recipient, campaign, state: str, ts: datetime) -> None:
    """Advance sent → delivered → read, never backwards."""
    if _RANK.get(state, 0) <= _RANK.get(recipient.status, 0):
        return

    recipient.status = state
    if state == "delivered":
        recipient.delivered_at = ts
        if campaign:
            campaign.delivered = (campaign.delivered or 0) + 1
    elif state == "read":
        recipient.read_at = ts
        if campaign:
            campaign.read = (campaign.read or 0) + 1


def _apply_failure(db: Session, recipient, campaign, status: dict) -> None:
    """
    Record an async failure and flag the number if Meta says it is not on WhatsApp.

    A recipient that already failed is left alone so a duplicate webhook cannot
    double-count the campaign's failure counter.
    """
    if recipient.status == "failed":
        return

    errors = status.get("errors") or []
    first  = errors[0] if errors else {}
    code   = first.get("code")
    title  = first.get("title") or first.get("message") or ""

    was_counted_sent = recipient.status in ("sent", "delivered", "read")

    recipient.status        = "failed"
    recipient.error_code    = str(code) if code is not None else None
    recipient.error_message = str(title)[:500] or None

    if campaign:
        campaign.failed = (campaign.failed or 0) + 1
        # It was counted as a success at dispatch; Meta has now contradicted that.
        if was_counted_sent and (campaign.sent or 0) > 0:
            campaign.sent -= 1

    if code in INVALID_NUMBER_CODES and campaign:
        _flag_invalid(db, campaign.company_id, recipient.mobile_no, str(code))


def _flag_invalid(db: Session, company_id, mobile_no: str, code: str) -> None:
    """
    Upsert into invalid_numbers so future campaigns can warn about this number.

    Company-scoped: a number failing for one business says nothing about another.
    """
    row = (
        db.query(InvalidNumber)
        .filter(
            InvalidNumber.company_id == company_id,
            InvalidNumber.mobile_no  == mobile_no,
        )
        .first()
    )
    now = datetime.now(timezone.utc)

    if row is None:
        db.add(InvalidNumber(
            company_id    = company_id,
            mobile_no     = mobile_no,
            error_code    = code,
            first_seen_at = now,
            last_seen_at  = now,
            occurrences   = 1,
        ))
        logger.info("Flagged invalid number %s for company %s (%s)", mobile_no, company_id, code)
    else:
        row.occurrences  = (row.occurrences or 0) + 1
        row.last_seen_at = now
        row.error_code   = code
