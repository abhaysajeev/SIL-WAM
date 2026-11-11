"""
The agent app's authentication dependency.

Deliberately separate from app/core/deps.py. That module's `require()` chains off
`get_current_user`, which decodes a JWT — the agent app carries an opaque token and a
device header instead, so it needs its own resolver rather than a special case bolted
into the shared one.

One query answers everything: which agent, which company, is the session live, does the
device still match, has it gone idle. That is not extra cost — the JWT path already
loads the user row on every request to re-check is_active and lockout, so this is the
same round trip carrying more information.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.errors import invalid_session
from app.core.config import settings
from app.core.database import get_db
from app.core.security import hash_refresh_token
from app.monitoring import middleware as capture

# auto_error=False so a missing header reaches us as None and becomes our own
# invalid_session, rather than FastAPI's bare 403 with a different body shape.
bearer = HTTPBearer(auto_error=False)


@dataclass
class AgentContext:
    """Everything an agent endpoint needs about its caller, resolved once."""
    session_id:   str
    agent_row_id: str
    agent_id:     str          # the display id, e.g. "AG01"
    user_id:      str
    username:     str
    full_name:    str
    company_id:   str | None
    company_name: str | None
    device_id:    str
    device_bound_at: datetime | None
    last_used_at: datetime


def get_agent_session(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    x_device_id: str | None = Header(default=None, alias="X-Device-Id"),
    db: Session = Depends(get_db),
) -> AgentContext:
    """
    Resolve the bearer token and device header into an AgentContext, or raise 401.

    Every failure returns the same `invalid_session` error. There is no refresh to
    attempt, so a 401 means exactly one thing to the app — drop the token, show login —
    and splitting the reasons would only invite it to branch on states it cannot act
    on differently.

    The device header is checked here, not just at login. Without that the binding
    holds for one second of a session that never expires, and a token lifted off the
    handset would work from anywhere.
    """
    if credentials is None or not credentials.credentials or not x_device_id:
        raise invalid_session()

    row = db.execute(
        text("""
            SELECT s.id            AS session_id,
                   s.device_id     AS session_device_id,
                   s.last_used_at,
                   s.expires_at,
                   a.id            AS agent_row_id,
                   a.agent_id,
                   a.device_bound_at,
                   a.is_active     AS agent_active,
                   u.id            AS user_id,
                   u.username,
                   u.full_name,
                   u.is_active     AS user_active,
                   u.locked_until,
                   u.company_id,
                   c.name          AS company_name
            FROM agent_sessions s
            JOIN agents  a ON a.id = s.agent_id
            JOIN users   u ON u.id = a.user_id
            LEFT JOIN companies c ON c.id = u.company_id
            WHERE s.token_hash = :token_hash
              AND s.revoked = FALSE
        """),
        {"token_hash": hash_refresh_token(credentials.credentials)},
    ).fetchone()

    if row is None:
        raise invalid_session()

    now = datetime.now(timezone.utc)

    # The binding must still hold for this exact handset.
    if row.session_device_id != x_device_id.strip():
        raise invalid_session()

    # Absolute expiry is normally NULL — honoured only if something set it.
    if row.expires_at and _aware(row.expires_at) < now:
        raise invalid_session()

    # Idle cutoff, applied lazily. No scheduler: a session that has gone quiet simply
    # stops resolving, and sweeping the dead rows is housekeeping done on login.
    last_used = _aware(row.last_used_at) if row.last_used_at else now
    if last_used < now - timedelta(days=settings.AGENT_SESSION_IDLE_DAYS):
        raise invalid_session()

    # Both switches, checked on every request — which is what makes revocation
    # immediate and an absolute token expiry unnecessary.
    if not row.agent_active or not row.user_active:
        raise invalid_session()

    if row.locked_until and _aware(row.locked_until) > now:
        raise invalid_session()

    _touch(db, row.session_id, last_used, now)
    capture.tag(company=row.company_name, agent=row.agent_id)

    return AgentContext(
        session_id=str(row.session_id),
        agent_row_id=str(row.agent_row_id),
        agent_id=row.agent_id,
        user_id=str(row.user_id),
        username=row.username,
        full_name=row.full_name,
        company_id=str(row.company_id) if row.company_id else None,
        company_name=row.company_name,
        device_id=row.session_device_id,
        device_bound_at=row.device_bound_at,
        last_used_at=last_used,
    )


def _aware(dt: datetime) -> datetime:
    """Postgres hands back naive datetimes in some paths; normalise to UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _touch(db: Session, session_id, last_used: datetime, now: datetime) -> None:
    """
    Move last_used_at forward, but only when it has gone stale.

    Writing on every request would mean a row UPDATE per read and pointless contention
    on the one row an active agent hits constantly. The idle cutoff is measured in
    days, so a few minutes of imprecision costs nothing.

    Also updates the agent's device_last_seen, which is what the admin panel shows.
    """
    if last_used > now - timedelta(minutes=settings.AGENT_SESSION_TOUCH_MINUTES):
        return

    db.execute(
        text("""
            UPDATE agent_sessions SET last_used_at = :now WHERE id = :sid
        """),
        {"now": now, "sid": str(session_id)},
    )
    db.execute(
        text("""
            UPDATE agents SET device_last_seen = :now
            WHERE id = (SELECT agent_id FROM agent_sessions WHERE id = :sid)
        """),
        {"now": now, "sid": str(session_id)},
    )
    db.commit()
