"""
PUT /agent-api/v1/devices/push-token — register this device for push notifications.

Call after login and whenever Firebase gives the app a new token (onNewToken). The token
is stored on this session, so logging out or being revoked stops pushes to this device
along with everything else. Android only (FCM).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.deps import AgentContext, get_agent_session
from app.core.database import get_db

router = APIRouter(prefix="/devices", tags=["Agent App"])


class PushTokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=512)
    platform: str = Field("android", pattern="^(android)$")


class PushTokenOut(BaseModel):
    registered: bool
    server_time: datetime


@router.put("/push-token", response_model=PushTokenOut)
def register_push_token(
    payload: PushTokenIn,
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    # A token belongs to one installation. If another (older) session still holds it —
    # the agent logged in again on the same phone — take it off that one, so the same
    # device is not pushed twice.
    db.execute(text("""UPDATE agent_sessions SET push_token = NULL
                        WHERE push_token = :t AND id <> CAST(:s AS uuid)"""),
               {"t": payload.token, "s": ctx.session_id})
    db.execute(text("""UPDATE agent_sessions
                          SET push_token = :t, push_platform = :p, push_token_updated_at = now()
                        WHERE id = CAST(:s AS uuid)"""),
               {"t": payload.token, "p": payload.platform, "s": ctx.session_id})
    db.commit()
    return PushTokenOut(registered=True, server_time=datetime.now(timezone.utc))


@router.delete("/push-token", response_model=PushTokenOut)
def remove_push_token(
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    """The agent turned notifications off in the app."""
    db.execute(text("UPDATE agent_sessions SET push_token = NULL WHERE id = CAST(:s AS uuid)"),
               {"s": ctx.session_id})
    db.commit()
    return PushTokenOut(registered=False, server_time=datetime.now(timezone.utc))
