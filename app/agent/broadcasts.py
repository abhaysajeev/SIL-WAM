"""
An agent starting a broadcast from the handset.

**This creates an ordinary campaign.** There is no parallel send path: the same
`broadcast_campaigns` row, the same recipient builder, the same opt-out and US-number
screening, the same queue with its concurrency, retries and receipt handling. The agent
app is simply another way to create a campaign, and everything downstream is unable to
tell the difference — which is the point. A second sending path would be a second place
for opt-out enforcement to be forgotten.

What *is* different is the front door: an agent may only use phonebooks and templates an
administrator mapped to them, and only up to `agent_max_recipients_per_send`. An admin
choosing a 2,000-contact phonebook meant to; an agent tapping one on a phone can reach
the same 2,000 people by accident.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent import errors
from app.agent.deps import AgentContext, get_agent_session
from app.core import broadcast_config, template_body
from app.core.database import get_db
from app.models.broadcast_campaign import BroadcastCampaign, BroadcastRecipient
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.services import broadcast_builder, broadcast_screening
from app.utils.error_logger import log_error

router = APIRouter(prefix="/broadcasts", tags=["Agent App"])


class BroadcastIn(BaseModel):
    phonebook_id: str
    # Optional subset of that phonebook — the rows the agent ticked. **Omit the field**
    # to send to the whole book; an empty list is refused rather than read as "all"
    # (see errors.nothing_selected). Ids come from GET /phonebooks, which already
    # returns every contact's id.
    contact_ids:  list[str] | None = None
    template_id:  str
    # Ordered values for {{1}}, {{2}}, … Must match the template's placeholder count,
    # which the app should re-read immediately before sending — see AGENT_API_TEMPLATES.
    params: list[str] = Field(default_factory=list)
    # Optional. The app's own rendering of the message, accepted so the composer's
    # preview and the chat bubble read identically. The server renders independently
    # and warns on a mismatch — see below.
    rendered: str | None = None
    name: str | None = None


class BroadcastOut(BaseModel):
    campaign_id:   str
    name:          str
    total:         int
    skipped:       int
    sendable:      int
    status:        str
    server_time:   datetime


@router.post("", response_model=BroadcastOut, status_code=201)
def start_broadcast(
    payload: BroadcastIn,
    background: BackgroundTasks,
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    """
    Create and dispatch a campaign to one of this agent's phonebooks.

    Returns as soon as the campaign is queued; nothing is sent inside this request.
    Progress is read from the chat threads, or from the campaign in the web panel.
    """
    if not ctx.company_id:
        raise errors.not_an_agent()

    # ── The agent may only use what an administrator gave them ──────────────
    # Checked by joining the mapping table rather than filtering afterwards, so a
    # phonebook id belonging to the right company but the wrong agent is a 404 and not
    # a partially-processed request.
    book = db.execute(
        text("""SELECT p.id, p.name FROM phonebooks p
                  JOIN agent_phonebooks ap ON ap.phonebook_id = p.id
                 WHERE ap.agent_id = :agent AND p.id = CAST(:pid AS uuid)
                   AND p.company_id = :company"""),
        {"agent": ctx.agent_row_id, "pid": payload.phonebook_id,
         "company": ctx.company_id},
    ).first()
    if book is None:
        raise errors.not_found("phonebook")

    # ── Narrow to the selected contacts, if any ─────────────────────────────
    # Validated here rather than in the builder because only this layer knows how to
    # phrase the failure for a handset, and the agent needs to be told *which* ids went
    # stale so the app can resync rather than guess.
    selected: list | None = None
    if payload.contact_ids is not None:
        if not payload.contact_ids:
            raise errors.nothing_selected()

        # Deduplicated first: the same contact ticked twice is one recipient, and
        # counting it twice would make the cap check below reject a legal selection.
        wanted = list(dict.fromkeys(payload.contact_ids))

        # Anything that is not a UUID is reported as stale rather than allowed to reach
        # the CAST below, where Postgres would raise and the agent would see a 500 for
        # what is really a bad id in the request.
        malformed = []
        for i in wanted:
            try:
                uuid.UUID(i)
            except (ValueError, AttributeError, TypeError):
                malformed.append(i)
        if malformed:
            raise errors.unknown_contacts(malformed)

        found = db.execute(
            text("""SELECT pc.id FROM phonebook_contacts pc
                     WHERE pc.phonebook_id = :pid
                       AND pc.id = ANY(CAST(:ids AS uuid[]))"""),
            {"pid": str(book.id), "ids": wanted},
        ).fetchall()
        found_ids = {str(r.id) for r in found}

        # Scoped to this phonebook, so an id belonging to a book this agent was never
        # given reads as unknown rather than quietly resolving.
        missing = [i for i in wanted if i not in found_ids]
        if missing:
            raise errors.unknown_contacts(missing)
        selected = [r.id for r in found]

    tpl_row = db.execute(
        text("""SELECT t.id FROM whatsapp_templates t
                  JOIN agent_templates at2 ON at2.template_id = t.id
                 WHERE at2.agent_id = :agent AND t.id = CAST(:tid AS uuid)
                   AND t.company_id = :company AND t.status = 'APPROVED'"""),
        {"agent": ctx.agent_row_id, "tid": payload.template_id,
         "company": ctx.company_id},
    ).first()
    if tpl_row is None:
        raise errors.not_found("template")

    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == tpl_row.id).first()

    expected = template_body.param_count(template.components)
    if len(payload.params) != expected:
        raise errors.param_count_mismatch(expected, len(payload.params))

    if db.query(WhatsAppAccount).filter(
            WhatsAppAccount.company_id == ctx.company_id).first() is None:
        raise errors.no_whatsapp_account()

    # The server renders from the approved components regardless. If the app supplied
    # its own string and they differ, the template changed at Meta after the app cached
    # it — the customer gets Meta's wording, so the discrepancy is logged rather than
    # silently resolved in favour of either.
    server_rendered = template_body.render(template.components, list(payload.params))
    if payload.rendered and payload.rendered.strip() != server_rendered.strip():
        log_error(
            "Agent broadcast render mismatch",
            f"POST /agent-api/v1/broadcasts agent={ctx.agent_id}",
            Exception("app-rendered text differs from the approved template"),
            request_data={"app": payload.rendered[:500], "server": server_rendered[:500]},
        )

    campaign = BroadcastCampaign(
        company_id    = ctx.company_id,
        name          = payload.name or f"{book.name} — {template.name}",
        template_id   = template.id,
        param_mode    = "same",
        status        = "draft",
        # The creator is what makes the per-send cap apply, both here and in the web
        # panel's /send. An admin pressing Send on this campaign later must not lift it.
        created_by_id = ctx.user_id,
    )
    db.add(campaign)
    db.flush()

    try:
        built = broadcast_builder.build_from_phonebooks(
            db, campaign, template,
            phonebook_ids=[book.id],
            params=list(payload.params),
            contact_ids=selected,
        )
    except broadcast_builder.BuildError as exc:
        db.rollback()
        raise errors.build_failed(exc.message)

    # The session has autoflush off, so the recipient rows built above are still
    # pending in memory. screen() and the bulk UPDATE below both query the table
    # directly and would see none of them — the campaign would screen nothing, update
    # nothing, and be refused as "nothing to send" with a perfectly good list sitting
    # unflushed. The web panel never hits this because it commits between building and
    # screening, in two separate requests.
    db.flush()

    # ── The cap, before anything is screened or queued ──────────────────────
    cap = broadcast_config.get(db, ctx.company_id).agent_max_recipients_per_send
    if cap and built["total"] > cap:
        db.rollback()
        raise errors.recipient_cap_exceeded(built["total"], cap)

    # Same screening as the web panel — opt-outs and US numbers become skipped rows
    # rather than sends.
    broadcast_screening.screen(db, campaign)
    campaign.status = "ready"

    # screen() sets statuses on ORM instances; the bulk UPDATE below reads the table
    # directly. Without this flush it would still see every row as "draft" and queue
    # the people screening had just excluded — opted-out contacts among them, which is
    # the one outcome the screening step exists to prevent.
    db.flush()

    sendable = db.query(BroadcastRecipient).filter(
        BroadcastRecipient.campaign_id == campaign.id,
        BroadcastRecipient.status == "draft",
    ).update({"status": "pending"}, synchronize_session=False)

    if not sendable:
        db.rollback()
        raise errors.nothing_to_send()

    campaign.status        = "sending"
    campaign.dispatched_at = datetime.now(timezone.utc)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        log_error("Agent broadcast failed",
                  f"POST /agent-api/v1/broadcasts agent={ctx.agent_id}", exc)
        raise errors.send_failed("The broadcast could not be queued.")

    # Start immediately rather than waiting for the next scheduler tick. Racing the
    # scheduler is safe — both claim rows with SKIP LOCKED.
    from app.api.campaigns_api import _kick_dispatch
    background.add_task(_kick_dispatch)

    db.refresh(campaign)
    return BroadcastOut(
        campaign_id=str(campaign.id),
        name=campaign.name,
        total=built["total"],
        skipped=campaign.skipped or 0,
        sendable=sendable,
        status=campaign.status,
        server_time=datetime.now(timezone.utc),
    )
