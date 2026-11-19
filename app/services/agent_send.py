"""
An agent replying to a customer from the app: text, a template, or an image/video/PDF.

    POST /agent-api/v1/threads/{thread_id}/messages     (app/agent/replies.py)

Every check runs on the server, in a fixed order, before anything is written:

    1  session + device                      (the route's dependency)
    2  the agent is a participant            → 404 not_found
    3  same client_msg_id seen before        → the original result (200), or
       same key, different request           → 409 idempotency_mismatch
    4  a teammate replied since last_seen_rev → 409 teammate_replied   (unless force)
    5  customer opted out                    → 403 recipient_opted_out
    6  company has a WhatsApp account        → 409 no_whatsapp_account
    7  text/media: 24-hour window open       → 409 window_closed       (templates: always ok)
    8  template: assigned + APPROVED now, values count and are clean, header file fits
    9  agent's daily limit                   → 403 daily_limit_reached

Then: the row is written `queued` and COMMITTED, and only then sent (`deliver`). If the
process dies before the send, the recovery sweep sends it. If it dies DURING the send,
Meta may already have delivered it and Meta offers no idempotency key — so a row stuck in
`sending` is marked `unconfirmed`, never re-sent. A customer never gets the same message
twice from a retry, whether the retry is the app's or ours.

Statuses: queued → sending → sent | failed (later delivered / read / failed by receipt).
Every status change bumps the thread's rev, so the app's sync shows it.
"""
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import template_body
from app.models.agent_timeline import AgentMessage, AgentThread
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.services import account_health, agent_timeline, media_assets, wa_sender

logger = logging.getLogger(__name__)

QUEUED_GRACE = timedelta(seconds=60)      # a queued, never-attempted row older → send it
SENDING_TIMEOUT = timedelta(minutes=5)    # an attempt older than this → unconfirmed

# Meta refuses template values with a newline, a tab, or more than 4 spaces in a row.
_BAD_PARAM = re.compile(r"[\n\t]| {5,}")


class SendRefused(Exception):
    def __init__(self, http: int, code: str, message: str, **extra):
        super().__init__(message)
        self.http, self.code, self.message, self.extra = http, code, message, extra


def request_hash(body: dict) -> str:
    """Stable hash of what was asked for (the retry key's content check)."""
    content = {k: body.get(k) for k in ("text", "template", "media")}
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


def find_existing(db: Session, agent_row_id, client_msg_id: str) -> AgentMessage | None:
    return (db.query(AgentMessage)
            .filter(AgentMessage.sent_by_agent_id == agent_row_id,
                    AgentMessage.client_msg_id == client_msg_id)
            .first())


def teammate_messages_since(db: Session, thread_id, agent_row_id, last_seen_rev: int) -> list:
    """Replies by OTHER agents the sender has not seen yet."""
    return (db.query(AgentMessage)
            .filter(AgentMessage.thread_id == thread_id,
                    AgentMessage.rev > last_seen_rev,
                    AgentMessage.direction == "outbound",
                    AgentMessage.kind.in_(("agent_reply", "template")),
                    AgentMessage.sent_by_agent_id.isnot(None),
                    AgentMessage.sent_by_agent_id != agent_row_id)
            .order_by(AgentMessage.rev).all())


def _opted_out(db: Session, company_id, mobile_no) -> bool:
    return db.execute(
        text("""SELECT 1 FROM messaging_optouts
                 WHERE mobile_no = :m AND company_id = :c LIMIT 1"""),
        {"m": mobile_no, "c": str(company_id)},
    ).first() is not None


def _template_for_agent(db: Session, agent_row_id, company_id, template_id) -> WhatsAppTemplate:
    row = db.execute(
        text("""SELECT t.id FROM whatsapp_templates t
                  JOIN agent_templates at2 ON at2.template_id = t.id
                 WHERE at2.agent_id = :agent AND t.id = CAST(:tid AS uuid)
                   AND t.company_id = :company"""),
        {"agent": str(agent_row_id), "tid": template_id, "company": str(company_id)},
    ).first() if _is_uuid(template_id) else None
    if row is None:
        raise SendRefused(404, "not_found", "That template was not found.")
    tpl = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == row.id).first()
    # Re-checked NOW: the app's template list may be hours old, and Meta pauses or
    # disables templates on quality signals in between.
    if tpl.status != "APPROVED":
        raise SendRefused(409, "template_unavailable",
                          f"Template '{tpl.name}' cannot be sent right now "
                          f"(WhatsApp status: {tpl.status}). Choose another.",
                          template_status=tpl.status)
    return tpl


def _is_uuid(v) -> bool:
    import uuid
    try:
        uuid.UUID(str(v))
        return True
    except (ValueError, TypeError):
        return False


def prepare(db: Session, *, thread: AgentThread, agent_row_id, company_id,
            body: dict, daily_limit: int, sent_today: int) -> dict:
    """
    Run checks 5–9 and return the row content to record. Raises SendRefused.
    (1–4 are the caller's: they need the request context and the idempotency lookup.)
    """
    if thread.mobile_no is None:
        raise SendRefused(409, "no_phone_number",
                          "This customer uses a WhatsApp username and has not shared their "
                          "number, so replies cannot be sent from the app yet.")
    if _opted_out(db, company_id, thread.mobile_no):
        raise SendRefused(403, "recipient_opted_out",
                          "This person has unsubscribed. Messages to them are blocked.")
    account = (db.query(WhatsAppAccount)
               .filter(WhatsAppAccount.company_id == company_id).first())
    if account is None or not account.access_token_encrypted:
        raise SendRefused(409, "no_whatsapp_account",
                          "Your company has no WhatsApp number connected. "
                          "Contact your administrator.")
    blocked = account_health.block_for(db, company_id)
    if blocked:
        raise SendRefused(503, "account_unavailable",
                          "Your company's WhatsApp account has a problem, so messages "
                          "cannot be sent right now. Contact your administrator.",
                          reason=blocked)

    window_open = agent_timeline.is_open(thread)
    tpl_in, media_in, text_in = body.get("template"), body.get("media"), body.get("text")

    if tpl_in:
        tpl = _template_for_agent(db, agent_row_id, company_id, tpl_in.get("template_id"))
        params = list(tpl_in.get("params") or [])
        expected = template_body.param_count(tpl.components)
        if len(params) != expected:
            raise SendRefused(400, "param_count_mismatch",
                              f"This template needs {expected} value(s); {len(params)} given.",
                              expected=expected, supplied=len(params))
        for i, p in enumerate(params, start=1):
            if not str(p).strip():
                raise SendRefused(400, "invalid_param", f"Value {{{{{i}}}}} is empty.", index=i)
            if _BAD_PARAM.search(str(p)):
                raise SendRefused(400, "invalid_param",
                                  f"Value {{{{{i}}}}} contains a line break, a tab or more "
                                  f"than 4 spaces in a row, which WhatsApp does not allow.",
                                  index=i)
        try:
            header = media_assets.header_media_for(db, account, tpl,
                                                   tpl_in.get("header_media_id"))
        except media_assets.MediaError as exc:
            raise SendRefused(_media_http(exc.code), exc.code, exc.message, **exc.extra)
        content = {
            "kind": "template",
            "body_rendered": template_body.render(tpl.components, params),
            "template_meta": {"template_id": str(tpl.id), "name": tpl.name,
                              "language": tpl.language, "params": params},
            "media": ({"format": header["format"].lower(), "media_id": header["media_id"],
                       "asset_id": header.get("asset_id"), "filename": header.get("filename")}
                      if header else None),
        }
    else:
        if not window_open:
            raise SendRefused(409, "window_closed",
                              "More than 24 hours since the customer last wrote. "
                              "Send a template instead.",
                              last_inbound_at=thread.last_inbound_at.isoformat()
                              if thread.last_inbound_at else None)
        if media_in:
            asset = media_assets.get_live(db, company_id, media_in.get("media_id"))
            if asset is None or asset.direction != "outbound":
                raise SendRefused(404, "media_not_found", "That file was not found. Upload it again.")
            if asset.deleted_at is not None or asset.expires_at <= datetime.now(timezone.utc):
                raise SendRefused(410, "media_expired", "That file has expired. Upload it again.")
            try:
                media_assets.ensure_meta_fresh(db, account, asset)
            except media_assets.MediaError as exc:
                raise SendRefused(502, exc.code, exc.message)
            caption = (media_in.get("caption") or "").strip() or None
            content = {
                "kind": "agent_reply",
                "body_rendered": caption,
                "template_meta": None,
                "media": {"format": asset.format.lower(), "media_id": asset.meta_media_id,
                          "asset_id": str(asset.id), "filename": asset.filename,
                          "caption": caption},
            }
        else:
            content = {"kind": "agent_reply", "body_rendered": text_in,
                       "template_meta": None, "media": None}

    if daily_limit and sent_today >= daily_limit:
        raise SendRefused(403, "daily_limit_reached",
                          f"You have sent {sent_today} messages in the last 24 hours, your "
                          f"limit is {daily_limit}.", sent_today=sent_today, limit=daily_limit)
    return content


def _media_http(code: str) -> int:
    from app.agent.errors import _MEDIA_STATUS
    return _MEDIA_STATUS.get(code, 400)


def deliver(db: Session, msg_id) -> AgentMessage | None:
    """
    Send one queued row to Meta and record the outcome. Safe to call twice: the claim
    (queued → sending) is a single conditional UPDATE, so only one caller sends.
    Commits.
    """
    claimed = db.execute(
        text("""UPDATE agent_messages SET status = 'sending', attempted_at = now()
                 WHERE id = :id AND status = 'queued' AND attempted_at IS NULL
                RETURNING thread_id"""),
        {"id": str(msg_id)},
    ).first()
    if claimed is None:
        db.rollback()
        return db.query(AgentMessage).filter(AgentMessage.id == msg_id).first()
    msg = db.query(AgentMessage).filter(AgentMessage.id == msg_id).first()
    msg.rev = agent_timeline.bump_rev(db, msg.thread_id)
    db.commit()

    thread = db.query(AgentThread).filter(AgentThread.id == msg.thread_id).first()
    account = (db.query(WhatsAppAccount)
               .filter(WhatsAppAccount.company_id == thread.company_id).first())
    try:
        result = _send(db, account, thread, msg)
    except Exception as exc:
        logger.exception("agent_send.deliver crashed for %s", msg_id)
        result = wa_sender.SendResult(ok=False, meta_message_id=None, error=str(exc))

    msg = db.query(AgentMessage).filter(AgentMessage.id == msg_id).first()
    now = datetime.now(timezone.utc)
    if result.ok:
        msg.status, msg.wamid, msg.sent_at = "sent", result.meta_message_id, now
    else:
        code = result.error_code or _code_in(result.error)
        msg.status = "failed"
        msg.error_code = code
        msg.error_message = (result.error or "")[:500] or None
        msg.failure_category = agent_timeline.failure_category(code)
    msg.rev = agent_timeline.bump_rev(db, msg.thread_id)
    db.commit()
    db.refresh(msg)
    return msg


def _code_in(err: str | None) -> str | None:
    m = re.search(r"\b(1\d{5})\b", err or "")
    return m.group(1) if m else None


def _send(db: Session, account, thread: AgentThread, msg: AgentMessage):
    to = thread.mobile_no
    if msg.kind == "template":
        meta = msg.template_meta or {}
        tpl = (db.query(WhatsAppTemplate)
               .filter(WhatsAppTemplate.id == meta.get("template_id")).first())
        if tpl is None:
            return wa_sender.SendResult(ok=False, meta_message_id=None,
                                        error="The template no longer exists")
        header = None
        if msg.media and msg.media.get("media_id"):
            header = {"format": msg.media.get("format", "image"),
                      "media_id": msg.media["media_id"],
                      "filename": msg.media.get("filename")}
        return wa_sender.send_template(account, tpl, list(meta.get("params") or []), to,
                                       header_media=header)
    if msg.media and msg.media.get("media_id"):
        m = msg.media
        return wa_sender.send_media(account, to, m.get("format"), m["media_id"],
                                    caption=m.get("caption"), filename=m.get("filename"))
    return wa_sender.send_text(account, to, msg.body_rendered or "")


def recover_stuck(db: Session, now: datetime | None = None) -> dict:
    """
    The crash-recovery sweep for agent sends.
      * queued, never attempted, older than a minute → send it now
      * sending, attempted more than 5 minutes ago   → unconfirmed (never re-sent)
    """
    now = now or datetime.now(timezone.utc)
    stuck = db.execute(
        text("""SELECT id FROM agent_messages
                 WHERE status = 'sending' AND attempted_at < :cut
                   AND sent_by_agent_id IS NOT NULL"""),
        {"cut": now - SENDING_TIMEOUT},
    ).fetchall()
    for r in stuck:
        msg = db.query(AgentMessage).filter(AgentMessage.id == r.id).first()
        msg.status = "unconfirmed"
        msg.error_message = ("The app lost contact while sending; this message may or "
                             "may not have been delivered.")
        msg.rev = agent_timeline.bump_rev(db, msg.thread_id)
        db.commit()

    queued = db.execute(
        text("""SELECT id FROM agent_messages
                 WHERE status = 'queued' AND attempted_at IS NULL
                   AND sent_by_agent_id IS NOT NULL AND client_msg_id IS NOT NULL
                   AND created_at < :cut
                 ORDER BY created_at LIMIT 50"""),
        {"cut": now - QUEUED_GRACE},
    ).fetchall()
    db.rollback()
    for r in queued:
        deliver(db, r.id)
    return {"unconfirmed": len(stuck), "resent": len(queued)}
