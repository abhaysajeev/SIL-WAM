"""
Agents and their app sessions.

    users
     └─ agents            (1:1 — an agent IS a user, with role_name = "agent")
         └─< agent_sessions

An agent is an ordinary company-scoped user carrying an extra row here. No separate
credentials store: they log in with the same username and password the web panel uses,
so lockout, disabled-account checks and password resets are inherited rather than
reimplemented for mobile.

Two things this table adds on top of a user:

  customer_id   an opaque licence key minted by an external licensing server. We match
                it, we never validate it — that server is not ours.
  device_id     the one phone this agent may use. NULL until the first successful
                login binds it (trust on first use), which avoids an admin having to
                read ANDROID_ID off a handset and type it into a form before the agent
                can log in at all.

Sessions are opaque tokens, not JWTs. See app/agent/auth.py for why.
"""
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.database import Base


class Agent(Base):
    """The agent-app configuration attached to one user."""
    __tablename__ = "agents"

    id      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,          # 1:1 — a user has at most one agent configuration
        index=True,
    )

    # Free-text identifier from the external agent system — deliberately not a FK, the
    # same reasoning phonebook_contacts.agent_id already carries: the id is minted
    # elsewhere and there is no agents-directory to point at.
    #
    # Uniqueness is per company, and company_id lives on users, so it is enforced in
    # agents_api rather than by a constraint here. A cross-table unique would need
    # either a denormalised company_id kept in sync forever, or a trigger; neither is
    # worth it for a field only an admin writes, one at a time, through a form.
    agent_id    = Column(String(100), nullable=False)

    # Checked against what the app sends. Opaque to us by design.
    customer_id = Column(String(100), nullable=False)

    # The bound handset. NULL means "not yet registered" — the next successful login
    # claims it. Practically this is Settings.Secure.ANDROID_ID, which survives a
    # reinstall but NOT a factory reset, hence the release-device admin action.
    device_id        = Column(String(200), nullable=True)
    device_bound_at  = Column(DateTime(timezone=True), nullable=True)
    device_last_seen = Column(DateTime(timezone=True), nullable=True)

    # App access, deliberately separate from users.is_active: it switches off the phone
    # without disabling the web user, for someone who moves off agent duty but still
    # needs the panel.
    is_active  = Column(Boolean, nullable=False, default=True, server_default="true")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # One device, one agent — the licensing unit. Partial, so the many unbound
        # agents (device_id NULL) do not collide with each other.
        #
        # Declared here AND hand-written in the migration: tests build the schema with
        # Base.metadata.create_all and never run alembic, so an index living only in
        # one of the two places is invisible in the other.
        Index(
            "uq_agents_device_id",
            "device_id",
            unique=True,
            postgresql_where=text("device_id IS NOT NULL"),
        ),
    )


class AgentTemplate(Base):
    """
    Which approved templates an agent may send.

    A join table rather than a JSONB array of ids on `agents`, because the foreign key
    does the integrity work: retire a template and the mapping disappears with it,
    instead of leaving a dead id in an array for something to trip over later. It also
    answers the reverse question — "which agents can send this template?" — which is
    the first thing asked when a template is about to be withdrawn.

    **Only the mapping is stored, never the template's structure.** Body text, parameter
    count and header format are read live from `whatsapp_templates` on every request.
    Meta templates change — `sync_templates` rewrites `components` wholesale, and a
    re-approval can add or remove a parameter — so a structure snapshotted at mapping
    time would drift out of agreement with what Meta will actually accept, and the only
    symptom would be a send rejected for a parameter-count mismatch.
    """
    __tablename__ = "agent_templates"

    id       = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id = Column(
        UUID(as_uuid=True),
        ForeignKey("whatsapp_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("agent_id", "template_id", name="uq_agent_template"),
    )


class AgentPhonebook(Base):
    """
    Which phonebooks an agent may work from.

    The same shape as AgentTemplate and for the same reasons — many-to-many, so it needs
    its own table rather than a column on `phonebooks`: one agent holds several books,
    and one book is shared by several agents.

    Nothing about the contacts is copied here. They stay in `phonebook_contacts` and are
    read live, so an import, an edit or a deletion reaches the app on its next call.
    """
    __tablename__ = "agent_phonebooks"

    id       = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    phonebook_id = Column(
        UUID(as_uuid=True),
        ForeignKey("phonebooks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("agent_id", "phonebook_id", name="uq_agent_phonebook"),
    )


class AgentSession(Base):
    """
    One live login from one device.

    Opaque rather than a JWT, because the thing a JWT buys — stateless validation — was
    already given up: deps loads the user row on every request to re-check is_active and
    lockout. Paying that query and getting a revocable, per-device session in return is
    strictly better than paying it and getting an unrevocable token.
    """
    __tablename__ = "agent_sessions"

    id       = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # sha256 of the token, never the token itself — a database dump must not hand over
    # live sessions. Same treatment refresh_tokens already gets.
    token_hash = Column(String(64), nullable=False, unique=True, index=True)

    # The device this token was minted for. Verified on every request, not just at
    # login: without that the binding holds for one second of a session that never
    # expires, and a token lifted off the phone works from anywhere.
    device_id  = Column(String(200), nullable=False)

    issued_at    = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    # NULL = valid until revoked. There is no absolute expiry: every control that
    # matters (disable the user, deactivate the agent, revoke the session) already
    # takes effect on the next request, so a timer would only log someone out
    # mid-shift for no triggering event. Idle expiry is applied lazily against
    # last_used_at instead.
    expires_at = Column(DateTime(timezone=True), nullable=True)

    revoked        = Column(Boolean, nullable=False, default=False, server_default="false")
    # logout | superseded | admin | device_release
    revoked_reason = Column(String(30), nullable=True)
    revoked_at     = Column(DateTime(timezone=True), nullable=True)

    # Support context — which build is this agent actually running.
    app_version = Column(String(50), nullable=True)
    os_version  = Column(String(50), nullable=True)

    __table_args__ = (
        # The hot path: find a live session by token. Partial so revoked rows, which
        # accumulate and are never looked up, stay out of the index.
        Index(
            "ix_agent_sessions_live",
            "token_hash",
            postgresql_where=text("revoked = false"),
        ),
    )
