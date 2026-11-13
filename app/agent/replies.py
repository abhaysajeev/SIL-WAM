"""
POST /agent-api/v1/threads/{thread_id}/messages — the agent replies.

One endpoint for all three kinds of reply; send exactly one of:

    "text":     "…"                                          (24-hour window only)
    "media":    {"media_id": "<POST /media id>", "caption"}   (24-hour window only)
    "template": {"template_id", "params": [...], "header_media_id"}   (any time)

`client_msg_id` (a UUID the app makes when the agent taps Send, and reuses on every
retry of that message) makes retries safe: the same key returns the original message
instead of sending again. See app/services/agent_send.py for the full order of checks.

Replaces POST /messages (which stays working for older app builds).
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.agent import errors
from app.agent.deps import AgentContext, get_agent_session
from app.agent.messages import _daily_count
from app.agent.threads import MessageOut, message_out
from app.core import broadcast_config
from app.core.database import get_db
from app.models.agent_timeline import AgentThread
from app.services import agent_send, agent_timeline

router = APIRouter(prefix="/threads", tags=["Agent App"])


class TemplateIn(BaseModel):
    template_id: str
    params: list[str] = Field(default_factory=list)
    header_media_id: str | None = None


class MediaIn(BaseModel):
    media_id: str
    caption: str | None = Field(None, max_length=1024)


class ReplyIn(BaseModel):
    client_msg_id: str
    # The highest rev the agent's screen shows. If a teammate replied after it, the send
    # is held back (409 teammate_replied) so the agent reads it first. Omit to skip.
    last_seen_rev: int | None = Field(None, ge=0)
    # Send anyway after seeing teammate_replied.
    force: bool = False
    text: str | None = Field(None, min_length=1, max_length=4096)
    template: TemplateIn | None = None
    media: MediaIn | None = None

    @field_validator("client_msg_id")
    @classmethod
    def _uuid(cls, v: str) -> str:
        try:
            return str(uuid.UUID(v))
        except (ValueError, TypeError):
            raise ValueError("client_msg_id must be a UUID")

    @model_validator(mode="after")
    def _exactly_one(self):
        given = [k for k in ("text", "template", "media") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError("Send exactly one of: text, template, media")
        if self.text is not None and not self.text.strip():
            raise ValueError("text is empty")
        return self


class ReplyOut(BaseModel):
    message:     MessageOut
    thread_rev:  int
    # True when this is the stored result of an earlier request with the same key.
    replayed:    bool
    server_time: datetime


@router.post("/{thread_id}/messages", response_model=ReplyOut, status_code=202)
def reply(
    thread_id: str,
    payload: ReplyIn,
    response: Response,
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    if not ctx.company_id:
        raise errors.not_an_agent()

    # 2. Participant — same rule as reading the thread.
    thread = None
    try:
        thread = (db.query(AgentThread)
                  .filter(AgentThread.id == uuid.UUID(thread_id),
                          AgentThread.company_id == ctx.company_id).first())
    except ValueError:
        pass
    if thread is None or not agent_timeline.is_visible(db, thread.id, ctx.agent_row_id):
        raise errors.not_found("conversation")

    body = payload.model_dump(exclude={"client_msg_id", "last_seen_rev", "force"})
    req_hash = agent_send.request_hash(body)

    # 3. Retry key.
    existing = agent_send.find_existing(db, ctx.agent_row_id, payload.client_msg_id)
    if existing is not None:
        return _replayed(existing, req_hash, response, db)

    # 4. Teammate replied since the agent last looked.
    if payload.last_seen_rev is not None and not payload.force:
        newer = agent_send.teammate_messages_since(db, thread.id, ctx.agent_row_id,
                                                   payload.last_seen_rev)
        if newer:
            raise HTTPException(409, detail={
                "code": "teammate_replied",
                "message": "A teammate has just replied to this customer. Read their "
                           "reply, then send again if you still want to.",
                "messages": [message_out(m).model_dump(mode="json") for m in newer],
            })

    # 5–9.
    tuning = broadcast_config.get(db, ctx.company_id)
    try:
        content = agent_send.prepare(
            db, thread=thread, agent_row_id=ctx.agent_row_id, company_id=ctx.company_id,
            body=body, daily_limit=tuning.agent_daily_message_limit,
            sent_today=_daily_count(db, ctx.agent_row_id) if tuning.agent_daily_message_limit else 0)
    except agent_send.SendRefused as exc:
        db.rollback()
        raise HTTPException(exc.http, detail={"code": exc.code, "message": exc.message,
                                              **exc.extra})

    # Durable first: the row is committed before anything goes to Meta.
    msg_id = agent_timeline.record_outbound(
        db, ctx.company_id, thread.mobile_no, thread_id=thread.id,
        kind=content["kind"], body_rendered=content["body_rendered"],
        template_meta=content["template_meta"], media=content["media"],
        status="queued", sent_by_agent_id=ctx.agent_row_id,
        client_msg_id=payload.client_msg_id, request_hash=req_hash,
    )
    if msg_id is None:
        # The unique (agent, client_msg_id) index lost a race with a concurrent retry of
        # the same message: answer with that one.
        db.rollback()
        existing = agent_send.find_existing(db, ctx.agent_row_id, payload.client_msg_id)
        if existing is not None:
            return _replayed(existing, req_hash, response, db)
        raise errors.send_failed("The message could not be saved. Try again.")
    db.commit()

    msg = agent_send.deliver(db, msg_id)
    return ReplyOut(message=message_out(msg), thread_rev=_thread_rev(db, thread.id),
                    replayed=False, server_time=datetime.now(timezone.utc))


def _replayed(existing, req_hash: str, response: Response, db: Session) -> ReplyOut:
    if existing.request_hash and existing.request_hash != req_hash:
        raise HTTPException(409, detail={
            "code": "idempotency_mismatch",
            "message": "This client_msg_id was already used for a different message. "
                       "Make a new id for each new message.",
        })
    response.status_code = 200
    return ReplyOut(message=message_out(existing), thread_rev=_thread_rev(db, existing.thread_id),
                    replayed=True, server_time=datetime.now(timezone.utc))


def _thread_rev(db: Session, thread_id) -> int:
    t = db.query(AgentThread).filter(AgentThread.id == thread_id).first()
    db.refresh(t)
    return t.rev or 0
