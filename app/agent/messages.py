"""
The agent's own send path: one free-text message to one customer.

Distinct from a broadcast, and deliberately so. A broadcast is bulk work handed to a
queue; this is a person waiting with a thumb over the send button, so it goes out inline
and reports what happened.

**Durability first.** The row is written and committed *before* the network call, then
updated with the outcome. A Meta hiccup then costs a retry, not the agent's typed text —
which they would have to notice was missing and retype from memory.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent import errors
from app.agent.deps import AgentContext, get_agent_session
from app.core import phone
from app.core.database import get_db
from app.models.agent_timeline import AgentMessage, AgentThread
from app.models.whatsapp import WhatsAppAccount
from app.schemas.phonebook import _validate_mobile
from app.services import agent_timeline, wa_sender
from app.utils.error_logger import log_error

router = APIRouter(prefix="/messages", tags=["Agent App"])


class SendMessageIn(BaseModel):
    mobile_no: str
    text: str = Field(min_length=1, max_length=4096)


class SendMessageOut(BaseModel):
    id:           str
    thread_id:    str
    seq:          int
    status:       str
    wamid:        str | None
    created_at:   datetime
    server_time:  datetime


def _daily_count(db: Session, agent_row_id: str) -> int:
    """
    How many messages this agent has sent in the last rolling 24 hours.

    Rolling, not since-midnight, because that is how Meta's own limits work — capacity
    returns gradually rather than all at once, and a cap that resets at midnight would
    let an agent spend tomorrow's allowance at 23:59.
    """
    # Broadcast rows also carry the sending agent (that is how a reply finds them), but
    # they have their own ceiling (agent_max_recipients_per_send) and never counted
    # here — so only the agent's own 1:1 messages are counted, as before.
    return db.execute(
        text("""SELECT count(*) FROM agent_messages
                 WHERE sent_by_agent_id = :aid
                   AND kind <> 'broadcast'
                   AND created_at > now() - interval '24 hours'"""),
        {"aid": agent_row_id},
    ).scalar() or 0


@router.post("", response_model=SendMessageOut, status_code=201)
def send_message(
    payload: SendMessageIn,
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    """
    Send one free-text message, inside the 24-hour service window.

    The window is checked **here**, whatever the app believes. Its countdown is a
    courtesy that stops an agent typing a paragraph they cannot send; this is the
    authority, because a device clock can be wrong by hours and Meta will reject the
    send with 131047 regardless of what the handset displayed.
    """
    if not ctx.company_id:
        raise errors.not_an_agent()

    try:
        mobile = _validate_mobile(payload.mobile_no)
    except ValueError:
        raise errors.not_found("contact")
    # Threads are keyed on the canonical form (918075571893); a phonebook may still hold
    # the short spelling. Same rule as the broadcast builder (app/core/phone.py).
    mobile = phone.try_normalize(mobile, phone.company_region(db, ctx.company_id)) or mobile

    account = (
        db.query(WhatsAppAccount)
        .filter(WhatsAppAccount.company_id == ctx.company_id)
        .first()
    )
    if account is None:
        raise errors.no_whatsapp_account()

    # Opt-out is a hard block, checked before anything is written. Messaging someone
    # who unsubscribed damages the quality rating of the number every other agent
    # shares (BROADCAST_DESIGN.md §9).
    if db.execute(
        text("""SELECT 1 FROM messaging_optouts
                 WHERE mobile_no = :m AND company_id = :c LIMIT 1"""),
        {"m": mobile, "c": ctx.company_id},
    ).first():
        raise errors.recipient_opted_out()

    from app.core import broadcast_config
    tuning = broadcast_config.get(db, ctx.company_id)
    if tuning.agent_daily_message_limit:
        sent = _daily_count(db, ctx.agent_row_id)
        if sent >= tuning.agent_daily_message_limit:
            raise errors.daily_limit_reached(sent, tuning.agent_daily_message_limit)

    thread = (
        db.query(AgentThread)
        .filter(AgentThread.company_id == ctx.company_id,
                AgentThread.mobile_no == mobile)
        .first()
    )
    # No thread means this customer has never written to us and we have never messaged
    # them — so the window has certainly never opened. Same refusal, not a 404: from
    # the agent's point of view the reason is identical.
    # Only a participant may write in a thread — the same rule as reading it. Asked
    # before the window, so a non-participant learns nothing about the customer's
    # recent activity.
    if thread is not None and not agent_timeline.is_visible(db, thread.id, ctx.agent_row_id):
        raise errors.not_found("conversation")
    if thread is None or not agent_timeline.is_open(thread):
        raise errors.window_closed(thread.last_inbound_at if thread else None)

    # 1. DURABLE FIRST. Written and committed before the network call, so a crash or a
    #    Meta timeout costs a retry rather than the agent's typed text.
    msg_id = agent_timeline.record_outbound(
        db, ctx.company_id, mobile,
        kind="agent_reply",
        body_rendered=payload.text,
        status="queued",
        sent_by_agent_id=ctx.agent_row_id,
    )
    if msg_id is None:
        raise errors.send_failed("The message could not be saved. Try again.")
    db.commit()

    # 2. Then attempt the send.
    result = wa_sender.send_text(account, mobile, payload.text)

    row = db.query(AgentMessage).filter(AgentMessage.id == msg_id).first()
    if result.ok:
        row.status  = "sent"
        row.wamid   = result.meta_message_id
        row.sent_at = datetime.now(timezone.utc)
    else:
        # Left recoverable rather than marked failed outright: a transient failure is
        # worth another attempt, and the agent can see it did not go and resend.
        row.status        = "failed"
        row.error_message = (result.error or "")[:500]
        log_error("Agent message send failed",
                  f"POST /agent-api/v1/messages agent={ctx.agent_id}",
                  Exception(result.error or "unknown"))
    db.commit()
    db.refresh(row)

    if not result.ok:
        raise errors.send_failed(result.error or "")

    return SendMessageOut(
        id=str(row.id), thread_id=str(row.thread_id), seq=row.seq,
        status=row.status, wamid=row.wamid, created_at=row.created_at,
        server_time=datetime.now(timezone.utc),
    )
