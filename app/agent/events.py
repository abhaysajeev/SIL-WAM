"""
GET /agent-api/v1/events — live "sync" events while the app is open (Server-Sent Events).

    event: sync
    data: {"type": "sync", "thread_id": "…", "rev": 57, "new_message": true}

On each event, fetch GET /threads/{thread_id}/messages?changed_after_rev=<what you hold>.
A `: ping` comment every 25s keeps proxies from closing the connection. Open it when the
app comes to the foreground, close it in the background (push takes over there).
Reconnect with backoff if it drops, and run a normal sync after reconnecting — events
sent while disconnected are not replayed; the rev sync is what guarantees completeness.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials

from app.agent.deps import AgentContext, bearer, get_agent_session
from app.core.database import SessionLocal
from app.services import live_events

router = APIRouter(prefix="/events", tags=["Agent App"])

HEARTBEAT_SECONDS = 25


def _authenticate(credentials, x_device_id) -> AgentContext:
    """
    The normal session check, on a session of its own that is closed straight away.
    Depending on get_db here would keep a pooled connection checked out for as long as
    the phone holds the stream open — minutes or hours, per open app.
    """
    db = SessionLocal()
    try:
        ctx = get_agent_session(credentials=credentials, x_device_id=x_device_id, db=db)
        db.commit()
        return ctx
    finally:
        db.close()


@router.get("")
async def events(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    x_device_id: str | None = Header(default=None, alias="X-Device-Id"),
):
    ctx = await run_in_threadpool(_authenticate, credentials, x_device_id)
    agent_id = str(ctx.agent_row_id)

    async def stream():
        entry = live_events.subscribe(agent_id)
        _loop, q = entry
        try:
            yield "retry: 5000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"event: sync\ndata: {json.dumps(event)}\n\n"
        finally:
            live_events.unsubscribe(agent_id, entry)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
