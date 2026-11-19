"""
Admin API for agent configuration — what the Agent Config tab on the user form talks to.

Separate from /api/users for two concrete reasons, both discovered in the existing code
rather than assumed:

  * `PUT /api/users/{id}` applies `model_dump(exclude_none=True)`, so a field can never
    be set back to NULL through it. Releasing a device is exactly a set-to-NULL, so it
    needs its own endpoint.
  * `form_view.html`'s collectPayload() sweeps every `.dv-input` carrying a `name`,
    including inside a hidden tab panel. Keeping agent fields out of that sweep — and
    off the users payload entirely — is what stops the two forms interfering.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import company_filter, get_current_user, require
from app.core import template_body
from app.models.agent import Agent, AgentPhonebook, AgentSession, AgentTemplate
from app.models.phonebook import Phonebook, PhonebookContact
from app.models.whatsapp import WhatsAppTemplate
from app.models.user import User
from app.services import agent_timeline
from app.utils.error_logger import log_error

router = APIRouter(
    prefix="/api/agents",
    tags=["Agents"],
    dependencies=[Depends(require("agents", "read"))],
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class AgentConfigIn(BaseModel):
    agent_id:    str = Field(..., min_length=1, max_length=100)
    customer_id: str = Field(..., min_length=1, max_length=100)
    is_active:   bool = True
    # The full set the agent may send. Sent on every save and applied as a replacement,
    # which is what a multi-select means: what is on screen is what should be stored.
    # Omitted (None) leaves the existing mapping alone, so a caller that does not manage
    # templates cannot wipe them by accident.
    template_ids: list[uuid.UUID] | None = None
    # Same contract as template_ids: the full set on every save, None to leave alone.
    phonebook_ids: list[uuid.UUID] | None = None

    @field_validator("agent_id", "customer_id")
    @classmethod
    def strip(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("must not be blank")
        return s


class AgentConfigOut(BaseModel):
    id:          uuid.UUID
    user_id:     uuid.UUID
    agent_id:    str
    customer_id: str
    is_active:   bool
    device_id:        str | None
    device_bound_at:  datetime | None
    device_last_seen: datetime | None
    created_at:  datetime | None

    model_config = ConfigDict(from_attributes=True)


class AgentTemplateOut(BaseModel):
    """
    A mapped template, as the admin tab shows it.

    `status` is carried so the tab can flag one that is no longer APPROVED — the agent
    app silently omits those, and without this an admin would have no way to see why an
    agent lost a template.
    """
    id:            uuid.UUID
    name:          str
    language:      str
    category:      str
    status:        str
    param_count:   int
    header_format: str | None


class AgentPhonebookOut(BaseModel):
    """A mapped phonebook, as the admin tab shows it."""
    id:            uuid.UUID
    name:          str
    contact_count: int


class AgentSessionOut(BaseModel):
    id:           uuid.UUID
    device_id:    str
    issued_at:    datetime | None
    last_used_at: datetime | None
    app_version:  str | None
    os_version:   str | None

    model_config = ConfigDict(from_attributes=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _owned_user(db: Session, user_id: uuid.UUID, current) -> User:
    """The target user, 404 if absent and 403 if they belong to another company."""
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404, "User not found")
    cid = company_filter(current)
    if cid and str(u.company_id) != cid:
        raise HTTPException(403, "Access denied")
    return u


def _owned_agent(db: Session, agent_row_id: uuid.UUID, current) -> Agent:
    a = db.query(Agent).filter(Agent.id == agent_row_id).first()
    if not a:
        raise HTTPException(404, "Agent configuration not found")
    _owned_user(db, a.user_id, current)
    return a


def _revoke_sessions(db: Session, agent_row_id, reason: str) -> int:
    return db.execute(
        text("""
            UPDATE agent_sessions
            SET revoked = TRUE, revoked_reason = :reason, revoked_at = now()
            WHERE agent_id = :aid AND revoked = FALSE
        """),
        {"aid": str(agent_row_id), "reason": reason},
    ).rowcount or 0


def _template_row(t: WhatsAppTemplate) -> AgentTemplateOut:
    comps = t.components or []
    return AgentTemplateOut(
        id=t.id, name=t.name, language=t.language, category=t.category, status=t.status,
        param_count=template_body.param_count(comps),
        header_format=template_body.header_format(comps),
    )


def _apply_template_mapping(db: Session, agent: Agent, company_id, wanted: list) -> None:
    """
    Replace the agent's mapped templates with `wanted`.

    Refuses a template from another company outright rather than skipping it quietly —
    a silent drop would leave the admin looking at a save that appeared to work and a
    list that is missing an entry, with nothing to explain the difference.
    """
    wanted_ids = {str(t) for t in wanted}
    if wanted_ids:
        found = db.query(WhatsAppTemplate).filter(
            WhatsAppTemplate.id.in_(wanted_ids),
            WhatsAppTemplate.company_id == company_id,
        ).all()
        if len(found) != len(wanted_ids):
            raise HTTPException(
                400,
                "One or more templates do not exist or belong to another company",
            )

    existing = {
        str(r.template_id): r
        for r in db.query(AgentTemplate).filter(AgentTemplate.agent_id == agent.id)
    }
    for tid, row in existing.items():
        if tid not in wanted_ids:
            db.delete(row)
    for tid in wanted_ids - existing.keys():
        db.add(AgentTemplate(agent_id=agent.id, template_id=uuid.UUID(tid)))


def _mapped_templates(db: Session, agent_row_id) -> list[AgentTemplateOut]:
    """
    Every mapped template, including ones no longer APPROVED.

    The agent app filters those out; the admin tab must not, or nobody can see why an
    agent quietly lost a template.
    """
    rows = (
        db.query(WhatsAppTemplate)
        .join(AgentTemplate, AgentTemplate.template_id == WhatsAppTemplate.id)
        .filter(AgentTemplate.agent_id == agent_row_id)
        .order_by(WhatsAppTemplate.name)
        .all()
    )
    return [_template_row(t) for t in rows]


def _phonebook_rows(db: Session, ids=None, agent_row_id=None, company_id=None):
    """
    Phonebooks with their contact counts, in one grouped query rather than one per row.

    Serves both the mapped list and the dropdown, which differ only in their filter.
    """
    q = (
        db.query(Phonebook, func.count(PhonebookContact.id).label("contact_count"))
        .outerjoin(PhonebookContact, PhonebookContact.phonebook_id == Phonebook.id)
        .group_by(Phonebook.id)
        .order_by(Phonebook.name)
    )
    if agent_row_id is not None:
        q = q.join(AgentPhonebook, AgentPhonebook.phonebook_id == Phonebook.id).filter(
            AgentPhonebook.agent_id == agent_row_id
        )
    if company_id is not None:
        q = q.filter(Phonebook.company_id == company_id)
    if ids is not None:
        q = q.filter(Phonebook.id.in_(ids))
    return [
        AgentPhonebookOut(id=p.id, name=p.name, contact_count=n) for p, n in q.all()
    ]


def _apply_phonebook_mapping(db: Session, agent: Agent, company_id, wanted: list) -> None:
    """
    Replace the agent's mapped phonebooks with `wanted`.

    Refuses a phonebook from another company outright rather than skipping it quietly —
    the same reasoning as templates: a silent drop leaves the admin looking at a save
    that appeared to work and a list missing an entry.
    """
    wanted_ids = {str(p) for p in wanted}
    if wanted_ids:
        found = db.query(Phonebook.id).filter(
            Phonebook.id.in_(wanted_ids),
            Phonebook.company_id == company_id,
        ).all()
        if len(found) != len(wanted_ids):
            raise HTTPException(
                400,
                "One or more phonebooks do not exist or belong to another company",
            )

    existing = {
        str(r.phonebook_id): r
        for r in db.query(AgentPhonebook).filter(AgentPhonebook.agent_id == agent.id)
    }
    for pid, row in existing.items():
        if pid not in wanted_ids:
            db.delete(row)
    for pid in wanted_ids - existing.keys():
        db.add(AgentPhonebook(agent_id=agent.id, phonebook_id=uuid.UUID(pid)))


# ── Read ──────────────────────────────────────────────────────────────────────

@router.get("/by-user/{user_id}", response_model=AgentConfigOut | None)
def get_agent_config(
    user_id: uuid.UUID,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The agent configuration for a user, or null if they are not set up as one."""
    _owned_user(db, user_id, user)
    return db.query(Agent).filter(Agent.user_id == user_id).first()


@router.get("/by-user/{user_id}/templates", response_model=list[AgentTemplateOut])
def get_mapped_templates(
    user_id: uuid.UUID,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """What this agent may send. Empty if not configured, or nothing mapped yet."""
    _owned_user(db, user_id, user)
    agent = db.query(Agent).filter(Agent.user_id == user_id).first()
    return _mapped_templates(db, agent.id) if agent else []


@router.get("/by-user/{user_id}/available-templates", response_model=list[AgentTemplateOut])
def get_available_templates(
    user_id: uuid.UUID,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    The approved templates of THIS USER'S company — what the mapping dropdown offers.

    Scoped to the user's own company rather than taking a company_id parameter, so a
    caller cannot browse another company's template catalogue by changing a query
    string. A user with no company (admin tier) has no catalogue to map from.
    """
    target = _owned_user(db, user_id, user)
    if not target.company_id:
        return []
    rows = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.company_id == target.company_id,
            WhatsAppTemplate.status == "APPROVED",
        )
        .order_by(WhatsAppTemplate.name)
        .all()
    )
    return [_template_row(t) for t in rows]


@router.get("/by-user/{user_id}/phonebooks", response_model=list[AgentPhonebookOut])
def get_mapped_phonebooks(
    user_id: uuid.UUID,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Which phonebooks this agent may work from."""
    _owned_user(db, user_id, user)
    agent = db.query(Agent).filter(Agent.user_id == user_id).first()
    return _phonebook_rows(db, agent_row_id=agent.id) if agent else []


@router.get("/by-user/{user_id}/available-phonebooks", response_model=list[AgentPhonebookOut])
def get_available_phonebooks(
    user_id: uuid.UUID,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    The phonebooks of THIS USER'S company — what the mapping dropdown offers.

    Scoped to the user's own company rather than taking a company_id parameter, so a
    caller cannot browse another company's contact lists by editing a query string.
    """
    target = _owned_user(db, user_id, user)
    if not target.company_id:
        return []
    return _phonebook_rows(db, company_id=target.company_id)


@router.get("/{agent_row_id}/sessions", response_model=list[AgentSessionOut])
def list_sessions(
    agent_row_id: uuid.UUID,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Live sessions only — a revoked one is history, not something to act on."""
    _owned_agent(db, agent_row_id, user)
    return (
        db.query(AgentSession)
        .filter(AgentSession.agent_id == agent_row_id, AgentSession.revoked.is_(False))
        .order_by(AgentSession.last_used_at.desc())
        .all()
    )


# ── Write ─────────────────────────────────────────────────────────────────────

@router.put("/by-user/{user_id}", response_model=AgentConfigOut)
def upsert_agent_config(
    user_id: uuid.UUID,
    payload: AgentConfigIn,
    request: Request,
    _perm=Depends(require("agents", "write")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create or update a user's agent configuration.

    Deliberately does NOT touch device_id. Binding happens on first login, and
    releasing has its own endpoint — see the module docstring.
    """
    target = _owned_user(db, user_id, user)

    # agent_id is unique per company. The constraint cannot live in the database
    # because company_id sits on users, not agents, and a cross-table unique would need
    # a denormalised copy kept in sync forever or a trigger. Only an admin writes this,
    # through a form, one at a time — a scoped check is proportionate.
    clash = db.execute(
        text("""
            SELECT a.id FROM agents a
            JOIN users u ON u.id = a.user_id
            WHERE a.agent_id = :agent_id
              AND a.user_id <> :user_id
              AND u.company_id IS NOT DISTINCT FROM :company_id
        """),
        {
            "agent_id": payload.agent_id,
            "user_id": str(user_id),
            "company_id": str(target.company_id) if target.company_id else None,
        },
    ).fetchone()
    if clash:
        raise HTTPException(409, f"Agent ID '{payload.agent_id}' is already used in this company")

    agent = db.query(Agent).filter(Agent.user_id == user_id).first()
    if agent is None:
        agent = Agent(user_id=user_id)
        db.add(agent)

    agent.agent_id    = payload.agent_id
    agent.customer_id = payload.customer_id
    agent.is_active   = payload.is_active

    # Templates are applied only when the caller sent the key at all — see the field's
    # comment. db.flush() first so a brand-new agent has an id to map against.
    if payload.template_ids is not None:
        db.flush()
        _apply_template_mapping(db, agent, target.company_id, payload.template_ids)

    if payload.phonebook_ids is not None:
        db.flush()
        _apply_phonebook_mapping(db, agent, target.company_id, payload.phonebook_ids)

    # Switching an agent off should also end whatever is running on their handset,
    # otherwise a live token keeps working until the next request happens to check.
    if not payload.is_active:
        _revoke_sessions(db, agent.id, "admin")
        # …and stop customer replies reaching them. Teammates who also contacted the
        # customer keep the chat; otherwise it is visible in the web panel only.
        # (Releasing a device does NOT do this: the same agent carries on with their
        # chats on the new handset.)
        agent_timeline.remove_agent_everywhere(db, agent.id)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        log_error("Agent config save failed", f"PUT /api/agents/by-user/{user_id}",
                  exc, request=request, user=str(user.id))
        raise HTTPException(500, "Could not save the agent configuration")

    db.refresh(agent)
    return agent


@router.post("/{agent_row_id}/release-device")
def release_device(
    agent_row_id: uuid.UUID,
    _perm=Depends(require("agents", "write")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Unbind the handset so the agent can register a new one on their next login.

    The routine fix for two situations that are otherwise dead ends: a replaced phone,
    and a factory reset — which changes ANDROID_ID and locks the agent out through no
    fault of their own. Also revokes live sessions, since the token they hold is bound
    to the device being released.
    """
    agent = _owned_agent(db, agent_row_id, user)

    released = agent.device_id
    agent.device_id = None
    agent.device_bound_at = None
    agent.device_last_seen = None
    revoked = _revoke_sessions(db, agent.id, "device_release")
    db.commit()

    return {
        "agent_id": str(agent.id),
        "released_device_id": released,
        "sessions_revoked": revoked,
    }


@router.post("/sessions/{session_id}/revoke", status_code=204)
def revoke_session(
    session_id: uuid.UUID,
    _perm=Depends(require("agents", "write")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """End one session without touching the device binding — for a lost phone."""
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if not session:
        raise HTTPException(404, "Session not found")
    _owned_agent(db, session.agent_id, user)

    if not session.revoked:
        session.revoked = True
        session.revoked_reason = "admin"
        session.revoked_at = datetime.now(session.issued_at.tzinfo if session.issued_at else None)
        db.commit()
