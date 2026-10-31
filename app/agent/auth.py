"""
Agent app authentication.

The APK posts four values — username, password, customer_id, device_id — and gets back
one opaque session token. No refresh token, no rotation, no expiry.

**Why not the JWT + refresh pair the web panel uses.** A refresh token there is
single-use with no grace window (api/auth.py): the UPDATE revokes it and returns the
successor in one statement. If that response is lost in transit — routine on the mobile
networks these agents work on — the server has already consumed the token and the agent
is logged out for good. No amount of client-side care fixes a lost response.

The usual reason to accept that risk is that a JWT is stateless and therefore
unrevocable, so it must be short-lived. This codebase already gave up statelessness:
`get_current_user` loads the user row on every request to re-check is_active and
lockout. We were paying the query and getting none of the benefit. An opaque session
resolves in that same query and is individually revocable — so a lost handset can be
cut off without disturbing anyone else's login.
"""
import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent import errors
from app.agent.deps import AgentContext, get_agent_session
from app.agent.schemas import (
    AgentIdentity,
    AgentLoginRequest,
    AgentSessionResponse,
    DeviceInfo,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.security import (
    dummy_verify,
    generate_refresh_token,
    hash_refresh_token,
    verify_password,
)
from app.models.agent import Agent, AgentSession
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["Agent App"])


@router.post("/login", response_model=AgentSessionResponse)
def agent_login(
    payload: AgentLoginRequest,
    db: Session = Depends(get_db),
):
    """
    Sign an agent in from the app.

    The order of the checks is itself the security property: nothing specific is said
    until the password has been proven, and everything after that point is said
    plainly. See errors.py.
    """
    user = db.query(User).filter(User.username == payload.username).first()

    # ── 1. Lockout, before spending bcrypt cycles ────────────────────────────
    if user and user.locked_until:
        locked_until = _aware(user.locked_until)
        if locked_until > datetime.now(timezone.utc):
            remaining = max(1, int((locked_until - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
            raise errors.account_locked(remaining)

    # ── 2. Credentials ───────────────────────────────────────────────────────
    # The dummy verify keeps an unknown username costing the same as a wrong password,
    # so timing does not answer a question the response body refuses to.
    if not user or not verify_password(payload.password, user.hashed_password):
        if not user:
            dummy_verify()
        else:
            _register_failed_attempt(db, user)
        raise errors.invalid_credentials()

    if not user.is_active:
        raise errors.account_disabled()

    # ── 3. Is this user set up for the app at all? ───────────────────────────
    # Locked FOR UPDATE: the device checks and the supersede below must not interleave
    # with a second login for the same agent, or two handsets could both bind, or two
    # live sessions could survive.
    agent = (
        db.query(Agent)
        .filter(Agent.user_id == user.id)
        .with_for_update()
        .first()
    )
    if agent is None or not agent.is_active:
        raise errors.not_an_agent()

    # ── 4. Licence ───────────────────────────────────────────────────────────
    if agent.customer_id != payload.customer_id:
        raise errors.licence_mismatch()

    # ── 5. Device ────────────────────────────────────────────────────────────
    newly_bound = _resolve_device(db, agent, payload.device_id)

    # Credentials were right, so clear the failure counter. Note this happens AFTER
    # the device and licence checks but only counts credential failures — a wrong
    # handset must never contribute toward locking a real account out.
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = datetime.now(timezone.utc)

    # ── 6. Mint the session ──────────────────────────────────────────────────
    raw_token = generate_refresh_token()
    _supersede_existing(db, agent.id)
    db.add(AgentSession(
        agent_id=agent.id,
        token_hash=hash_refresh_token(raw_token),
        device_id=payload.device_id,
        app_version=payload.app_version,
        os_version=payload.os_version,
    ))

    # Housekeeping, not correctness: idle sessions stop resolving on their own, this
    # just stops the dead rows accumulating. Same 1-in-100 sweep api/auth.py uses,
    # which is why there is no scheduler for it.
    if random.randint(1, 100) == 1:
        db.execute(text("""
            DELETE FROM agent_sessions
            WHERE revoked = TRUE
               OR last_used_at < now() - make_interval(days => :idle_days)
        """), {"idle_days": settings.AGENT_SESSION_IDLE_DAYS})

    db.commit()
    db.refresh(agent)

    company_name = db.execute(
        text("SELECT name FROM companies WHERE id = :cid"),
        {"cid": str(user.company_id)},
    ).scalar() if user.company_id else None

    return AgentSessionResponse(
        token=raw_token,
        expires_at=None,
        server_time=datetime.now(timezone.utc),
        agent=AgentIdentity(
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            agent_id=agent.agent_id,
            company_id=user.company_id,
            company_name=company_name,
        ),
        device=DeviceInfo(
            device_id=agent.device_id,
            bound_at=agent.device_bound_at,
            newly_bound=newly_bound,
        ),
    )


@router.get("/session", response_model=AgentSessionResponse)
def agent_session(ctx: AgentContext = Depends(get_agent_session)):
    """
    Confirm the session is still good, and re-sample the clock.

    The app's cold-start check. It returns no token — the caller already holds one —
    but it does return a fresh `server_time`, which is what the 24-hour window
    countdown calibrates against before the first thread list renders.
    """
    return AgentSessionResponse(
        token=None,
        expires_at=None,
        server_time=datetime.now(timezone.utc),
        agent=AgentIdentity(
            user_id=ctx.user_id,
            username=ctx.username,
            full_name=ctx.full_name,
            agent_id=ctx.agent_id,
            company_id=ctx.company_id,
            company_name=ctx.company_name,
        ),
        device=DeviceInfo(
            device_id=ctx.device_id,
            bound_at=ctx.device_bound_at,
            newly_bound=False,        # only ever true on the login that claimed it
        ),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def agent_logout(
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    """Revoke this session only. The device stays bound — this is a sign-out, not a release."""
    db.execute(
        text("""
            UPDATE agent_sessions
            SET revoked = TRUE, revoked_reason = 'logout', revoked_at = now()
            WHERE id = :sid
        """),
        {"sid": ctx.session_id},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _register_failed_attempt(db: Session, user: User) -> None:
    """
    Count a wrong password, and lock the account at the threshold.

    Only reached from the credential branch. A licence or device failure deliberately
    never lands here: locking out an agent whose handset was replaced would turn a
    support call into a worse one.
    """
    user.failed_login_attempts += 1
    if user.failed_login_attempts >= settings.MAX_LOGIN_ATTEMPTS:
        user.locked_until = (
            datetime.now(timezone.utc) + timedelta(minutes=settings.LOCKOUT_MINUTES)
        )
    db.commit()


def _resolve_device(db: Session, agent: Agent, device_id: str) -> bool:
    """
    Bind, accept or refuse this handset. Returns True if it was bound just now.

    Trust on first use: an unbound agent claims the device they first sign in from.
    The alternative — an admin reading ANDROID_ID off the handset and typing it into a
    form before the agent can log in at all — does not survive contact with a rollout.
    """
    if agent.device_id == device_id:
        agent.device_last_seen = datetime.now(timezone.utc)
        return False

    if agent.device_id is not None:
        raise errors.device_mismatch(agent.device_id)

    # Unbound. Confirm nobody else holds this handset before claiming it — the partial
    # unique index is the real authority, this check exists to produce a readable
    # error instead of an IntegrityError.
    taken = (
        db.query(Agent.id)
        .filter(Agent.device_id == device_id, Agent.id != agent.id)
        .first()
    )
    if taken:
        raise errors.device_already_registered()

    now = datetime.now(timezone.utc)
    agent.device_id = device_id
    agent.device_bound_at = now
    agent.device_last_seen = now
    return True


def _supersede_existing(db: Session, agent_row_id) -> None:
    """
    Retire any live session for this agent.

    One agent, one device, therefore one live session. Without this, reinstalling the
    app without logging out first would leave an orphan token valid indefinitely, and
    the admin's session list would stop describing reality.
    """
    db.execute(
        text("""
            UPDATE agent_sessions
            SET revoked = TRUE, revoked_reason = 'superseded', revoked_at = now()
            WHERE agent_id = :aid AND revoked = FALSE
        """),
        {"aid": str(agent_row_id)},
    )
