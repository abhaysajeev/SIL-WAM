"""
GET /api/stream — Server-Sent Events for real-time message push.

Auth: JWT as ?token= query param (EventSource cannot send Authorization headers).
Cursor: ISO-8601 datetime in ?cursor= or Last-Event-ID header (header wins on reconnect).

The stream emits two event types:
  new_message   — a new Message row (inbound or outbound)
  status_update — delivery/read status changed on an outbound message

A `: heartbeat` comment is sent every 15 s to keep the TCP connection alive
without triggering the browser's `message` handler.

DB is polled every 2 s using asyncio.to_thread so the sync SQLAlchemy session
never blocks the event loop.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.core.database import SessionLocal
from app.core.security import decode_access_token
from app.models.conversation import Conversation, Message
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

router = APIRouter(tags=["SSE"])

_POLL_INTERVAL   = 2    # seconds between DB polls
_HEARTBEAT_EVERY = 15   # seconds between keepalive comments


# ── Auth helper ───────────────────────────────────────────────────────────────

def _authenticate(token: str) -> Optional[object]:
    """Decode JWT and return the user row, or None if invalid."""
    if not token:
        return None
    user_id = decode_access_token(token)
    if not user_id:
        return None
    db = SessionLocal()
    try:
        from sqlalchemy import text
        row = db.execute(
            text("""
                SELECT u.id, u.is_active, u.company_id,
                       r.name AS role_name
                FROM users u
                LEFT JOIN roles r ON r.id = u.role_id
                WHERE u.id = :id
            """),
            {"id": user_id},
        ).fetchone()
        if not row or not row.is_active:
            return None
        return row
    finally:
        db.close()


# ── DB queries (run in thread) ────────────────────────────────────────────────

def _fetch_new_messages(db, company_id: Optional[str], since: datetime) -> list[dict]:
    """Return messages created after `since`, scoped to company if set."""
    q = (
        db.query(Message, Conversation.id.label("conv_id"))
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(Message.created_at > since)
        .order_by(Message.created_at.asc())
    )
    if company_id:
        q = q.filter(Conversation.company_id == company_id)

    rows = q.all()
    result = []
    for msg, conv_id in rows:
        result.append({
            "type":            "new_message",
            "id":              str(msg.id),
            "conversation_id": str(conv_id),
            "wamid":           msg.wamid,
            "direction":       msg.direction,
            "message_type":    msg.message_type,
            "content":         msg.content,
            "status":          msg.status,
            "is_flow_message": msg.is_flow_message,
            "created_at":      msg.created_at.isoformat() if msg.created_at else None,
        })
    return result


def _fetch_status_changes(db, company_id: Optional[str], since: datetime) -> list[dict]:
    """Return messages whose status was updated after `since`."""
    q = (
        db.query(Message, Conversation.id.label("conv_id"))
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(
            Message.direction == "outbound",
            Message.status.in_(["delivered", "read", "failed"]),
        )
        .filter(
            (Message.delivered_at > since) |
            (Message.read_at > since)
        )
        .order_by(Message.created_at.asc())
    )
    if company_id:
        q = q.filter(Conversation.company_id == company_id)

    rows = q.all()
    result = []
    for msg, conv_id in rows:
        result.append({
            "type":            "status_update",
            "id":              str(msg.id),
            "conversation_id": str(conv_id),
            "wamid":           msg.wamid,
            "status":          msg.status,
        })
    return result


def _poll(company_id: Optional[str], since: datetime):
    db = SessionLocal()
    try:
        msgs    = _fetch_new_messages(db, company_id, since)
        updates = _fetch_status_changes(db, company_id, since)
        return msgs, updates
    finally:
        db.close()


# ── SSE event builders ────────────────────────────────────────────────────────

def _event(payload: dict, event_id: str) -> str:
    return f"id: {event_id}\ndata: {json.dumps(payload)}\n\n"


def _heartbeat() -> str:
    return ": heartbeat\n\n"


# ── Main stream generator ─────────────────────────────────────────────────────

async def _stream(request: Request, company_id: Optional[str], cursor: datetime) -> AsyncGenerator[str, None]:
    since          = cursor
    ticks_since_hb = 0  # in poll intervals

    try:
        while True:
            # Client disconnected?
            if await request.is_disconnected():
                break

            msgs, updates = await asyncio.to_thread(_poll, company_id, since)

            latest_ts = since
            for ev in [*msgs, *updates]:
                ts_str = ev.get("created_at") or ev.get("updated_at") or since.isoformat()
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts > latest_ts:
                        latest_ts = ts
                except (TypeError, ValueError):
                    pass
                yield _event(ev, ev_id := ts_str)

            if latest_ts > since:
                since = latest_ts

            ticks_since_hb += 1
            if ticks_since_hb * _POLL_INTERVAL >= _HEARTBEAT_EVERY:
                yield _heartbeat()
                ticks_since_hb = 0

            await asyncio.sleep(_POLL_INTERVAL)

    except asyncio.CancelledError:
        pass
    except GeneratorExit:
        pass
    except Exception as exc:
        log_error("SSE stream crashed", "GET /api/stream", exc)


# ── Route ─────────────────────────────────────────────────────────────────────

@router.get("/api/stream")
async def sse_stream(
    request: Request,
    token:  str          = Query(..., description="JWT access token"),
    cursor: Optional[str] = Query(None, description="ISO-8601 datetime of last seen message"),
):
    user = _authenticate(token)
    if not user:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    company_id = str(user.company_id) if user.company_id else None

    # Cursor priority: Last-Event-ID header (reconnect) > ?cursor= param > now
    raw_cursor = request.headers.get("Last-Event-ID") or cursor
    if raw_cursor:
        try:
            ts = datetime.fromisoformat(raw_cursor)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            ts = datetime.now(timezone.utc)
    else:
        ts = datetime.now(timezone.utc)

    return StreamingResponse(
        _stream(request, company_id, ts),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )
