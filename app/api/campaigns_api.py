"""
Broadcast campaign CRUD, recipient building, screening and dispatch.

The send itself is not here — POST /{id}/send only flips the campaign to "sending" and
returns. broadcast_scheduler picks it up, exactly as the transactional pipeline separates
ingest from send_scheduler. A request that waited on 1,000 Meta calls would time out and
leave no way to resume.

Case 1 only for now (`param_mode = "same"`): recipients come from phonebooks and one
set of parameters is applied to all of them. Case 2 (per-row CSV) will write the same
recipient rows, so nothing downstream needs to change.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from app.core import template_body
from app.core.column_mapper import campaign_fields, field_catalogue, suggest_mapping
from app.core.database import get_db
from app.core.deps import company_filter, get_current_user, require
from app.core.pagination import paginate
from app.models.broadcast_campaign import (
    MAX_RECIPIENTS_PER_CAMPAIGN,
    BroadcastCampaign,
    BroadcastRecipient,
)
from app.models.phonebook import Phonebook, PhonebookContact
from app.models.whatsapp import WhatsAppTemplate
from app.schemas.phonebook import _validate_mobile
from app.services import broadcast_screening
from app.utils.error_logger import log_error
from sqlalchemy.exc import IntegrityError

router = APIRouter(
    prefix="/api/campaigns",
    tags=["Broadcast Campaigns"],
    dependencies=[Depends(require("campaigns", "read"))],
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class CampaignCreate(BaseModel):
    company_id:  uuid.UUID
    template_id: uuid.UUID
    # "same"    — one set of values for everyone, recipients from PhoneBooks
    # "per_row" — each recipient carries their own values, from a CSV
    param_mode: str = "same"
    # Optional: a name is a label for reports, not information the user has that we
    # do not. Omitted means auto-generated from the template and date.
    name: str | None = None

    @field_validator("name")
    @classmethod
    def blank_is_absent(cls, v: str | None) -> str | None:
        return (v or "").strip() or None

    @field_validator("param_mode")
    @classmethod
    def known_mode(cls, v: str) -> str:
        if v not in ("same", "per_row"):
            raise ValueError("param_mode must be 'same' or 'per_row'")
        return v


class RecipientBuild(BaseModel):
    """Case 1: which phonebooks, and the parameter values everyone receives."""
    phonebook_ids: list[uuid.UUID]
    params:   list[str] = []
    stop_on_error: bool = False


class CampaignOut(BaseModel):
    id:          uuid.UUID
    company_id:  uuid.UUID
    name:        str
    template_id: uuid.UUID | None
    status:      str
    param_mode:  str
    shared_params: list[str] | None
    stop_on_error: bool
    total: int
    sent: int
    delivered: int
    read: int
    failed: int
    skipped: int
    created_at: datetime | None
    dispatched_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _owned(db: Session, campaign_id: uuid.UUID, user) -> BroadcastCampaign:
    """Fetch a campaign, 404 if absent and 403 if it belongs to another company."""
    c = db.query(BroadcastCampaign).filter(BroadcastCampaign.id == campaign_id).first()
    if not c:
        raise HTTPException(404, "Campaign not found")
    cid = company_filter(user)
    if cid and str(c.company_id) != cid:
        raise HTTPException(403, "Access denied")
    return c


def _auto_name(db: Session, company_id: uuid.UUID, template: WhatsAppTemplate) -> str:
    """
    Label a campaign from its template and the date.

    Several sends of one template on one day are normal, so a counter is appended
    rather than letting the list show three identical rows.
    """
    base = f"{template.name} — {datetime.now(timezone.utc):%d %b %Y}"
    taken = {
        n for (n,) in db.query(BroadcastCampaign.name).filter(
            BroadcastCampaign.company_id == company_id,
            BroadcastCampaign.name.like(f"{base}%"),
        )
    }
    if base not in taken:
        return base
    n = 2
    while f"{base} ({n})" in taken:
        n += 1
    return f"{base} ({n})"


def _template_for(db: Session, campaign: BroadcastCampaign) -> WhatsAppTemplate:
    t = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == campaign.template_id).first()
    if not t:
        raise HTTPException(409, "The template for this campaign no longer exists")
    return t


# ── Campaign CRUD ─────────────────────────────────────────────────────────────

@router.get("/", response_model=list[CampaignOut])
def list_campaigns(user=Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(BroadcastCampaign)
    cid = company_filter(user)
    if cid:
        q = q.filter(BroadcastCampaign.company_id == uuid.UUID(cid))
    return q.order_by(BroadcastCampaign.created_at.desc()).all()


@router.post("/", response_model=CampaignOut, status_code=201)
def create_campaign(
    payload: CampaignCreate,
    _perm=Depends(require("campaigns", "create")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cid = company_filter(user)
    if cid and str(payload.company_id) != cid:
        raise HTTPException(403, "Access denied")

    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id         == payload.template_id,
        WhatsAppTemplate.company_id == payload.company_id,
        WhatsAppTemplate.status     == "APPROVED",
    ).first()
    if not template:
        raise HTTPException(404, "Approved template not found for this company")

    campaign = BroadcastCampaign(
        company_id    = payload.company_id,
        name          = payload.name or _auto_name(db, payload.company_id, template),
        template_id   = template.id,
        param_mode    = payload.param_mode,
        status        = "draft",
        created_by_id = user.id,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(
    campaign_id: uuid.UUID,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _owned(db, campaign_id, user)


@router.delete("/{campaign_id}", status_code=204)
def delete_campaign(
    campaign_id: uuid.UUID,
    _perm=Depends(require("campaigns", "delete")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    campaign = _owned(db, campaign_id, user)
    # A dispatched campaign is a record of messages that really went out.
    if campaign.status in ("sending", "dispatched", "settled"):
        raise HTTPException(409, "A campaign that has started sending cannot be deleted")
    db.delete(campaign)
    db.commit()


# ── Recipients ────────────────────────────────────────────────────────────────

@router.post("/{campaign_id}/recipients")
def build_recipients(
    campaign_id: uuid.UUID,
    payload: RecipientBuild,
    _perm=Depends(require("campaigns", "write")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Resolve the selected lists into recipient rows.

    Rebuilding replaces the previous set, so a user can change their mind about which
    lists to include. Refused once sending has started.
    """
    campaign = _owned(db, campaign_id, user)
    if campaign.status not in ("draft", "screening", "ready"):
        raise HTTPException(409, f"Cannot rebuild recipients while campaign is '{campaign.status}'")
    if campaign.param_mode != "same":
        raise HTTPException(409, "This campaign takes its recipients from a CSV, not PhoneBooks")

    template = _template_for(db, campaign)
    expected = template_body.param_count(template.components)
    if len(payload.params) != expected:
        raise HTTPException(
            400,
            f"This template needs {expected} parameter(s); {len(payload.params)} supplied",
        )

    lists = db.query(Phonebook).filter(
        Phonebook.id.in_(payload.phonebook_ids),
        Phonebook.company_id == campaign.company_id,
    ).all()
    if len(lists) != len(set(payload.phonebook_ids)):
        raise HTTPException(404, "One or more phonebooks were not found for this company")

    contacts = db.query(PhonebookContact).filter(
        PhonebookContact.phonebook_id.in_([l.id for l in lists])
    ).all()

    # Dedup across lists — the same number in two lists is one recipient. Meta bills
    # unique recipients, and being messaged twice by one campaign reads as a bug.
    # First occurrence wins, which also decides whose agent_id is snapshotted.
    seen: dict[str, PhonebookContact] = {}
    for c in contacts:
        try:
            mobile = _validate_mobile(c.mobile_no)
        except ValueError:
            continue          # unusable number — silently excluded, never sent
        seen.setdefault(mobile, c)

    if len(seen) > MAX_RECIPIENTS_PER_CAMPAIGN:
        raise HTTPException(
            400,
            f"{len(seen)} recipients exceeds the current limit of "
            f"{MAX_RECIPIENTS_PER_CAMPAIGN} per campaign",
        )

    db.query(BroadcastRecipient).filter(
        BroadcastRecipient.campaign_id == campaign.id
    ).delete(synchronize_session=False)

    for mobile, contact in seen.items():
        db.add(BroadcastRecipient(
            campaign_id   = campaign.id,
            mobile_no     = mobile,
            customer_name = contact.customer_name,
            agent_id      = contact.agent_id,
            params        = list(payload.params),
            status        = "draft",
        ))

    campaign.shared_params = list(payload.params)
    campaign.stop_on_error = payload.stop_on_error
    campaign.total   = len(seen)
    campaign.status  = "screening"
    campaign.skipped = 0

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        log_error("Campaign recipient build failed", f"POST /api/campaigns/{campaign_id}/recipients", exc)
        raise HTTPException(500, "Could not build the recipient list")

    return {
        "campaign_id": str(campaign.id),
        "total": len(seen),
        "duplicates_removed": len(contacts) - len(seen),
    }


@router.get("/{campaign_id}/recipients")
def list_recipients(
    campaign_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Paginated — the preview table is a query over these rows, not a parsed file."""
    campaign = _owned(db, campaign_id, user)
    q = (
        db.query(BroadcastRecipient)
        .filter(BroadcastRecipient.campaign_id == campaign.id)
        .order_by(BroadcastRecipient.mobile_no)
    )
    page = paginate(q, request, per_page=50)
    return {
        "total": page.total,
        "page": page.page,
        "pages": page.pages,
        "items": [
            {
                "id": str(r.id),
                "mobile_no": r.mobile_no,
                "customer_name": r.customer_name,
                "agent_id": r.agent_id,
                "params": r.params,
                "status": r.status,
                "skip_reason": r.skip_reason,
                "error_code": r.error_code,
                "error_message": r.error_message,
            }
            for r in page.items
        ],
    }


# ── Screening and dispatch ────────────────────────────────────────────────────

@router.post("/{campaign_id}/screen")
def screen_campaign(
    campaign_id: uuid.UUID,
    _perm=Depends(require("campaigns", "write")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    campaign = _owned(db, campaign_id, user)
    if campaign.status not in ("screening", "ready", "draft"):
        raise HTTPException(409, f"Cannot screen a campaign that is '{campaign.status}'")

    result = broadcast_screening.screen(db, campaign)
    campaign.status = "ready"
    db.commit()
    return result.as_dict()


@router.post("/{campaign_id}/send")
def send_campaign(
    campaign_id: uuid.UUID,
    background: BackgroundTasks,
    _perm=Depends(require("campaigns", "write")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Hand the campaign to the worker.

    Flips screened recipients to "pending" and the campaign to "sending", then returns.
    Nothing is sent inside this request.
    """
    campaign = _owned(db, campaign_id, user)
    if campaign.status != "ready":
        raise HTTPException(409, "Screen the campaign before sending")
    _template_for(db, campaign)      # 409 early rather than failing every recipient

    pending = (
        db.query(BroadcastRecipient)
        .filter(
            BroadcastRecipient.campaign_id == campaign.id,
            BroadcastRecipient.status == "draft",
        )
        .update({"status": "pending"}, synchronize_session=False)
    )
    if not pending:
        raise HTTPException(409, "No sendable recipients — every row was skipped")

    campaign.status = "sending"
    campaign.dispatched_at = datetime.now(timezone.utc)
    db.commit()

    # Start immediately rather than waiting up to a full scheduler tick. Runs after
    # the response is sent, so the caller is never blocked on Meta. Racing the
    # scheduler is safe — both claim rows with SKIP LOCKED, which is why the claim
    # was written that way.
    background.add_task(_kick_dispatch)

    return {"campaign_id": str(campaign.id), "queued": pending}


def _kick_dispatch() -> None:
    """Dispatch now, in a session of this task's own."""
    from app.core.database import SessionLocal
    from app.services import broadcast_scheduler

    session = SessionLocal()
    try:
        broadcast_scheduler.dispatch_pending(session)
    except Exception as exc:
        session.rollback()
        log_error("Immediate broadcast dispatch failed", "campaigns_api._kick_dispatch", exc)
    finally:
        session.close()


@router.post("/{campaign_id}/cancel")
def cancel_campaign(
    campaign_id: uuid.UUID,
    _perm=Depends(require("campaigns", "write")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Stop a running campaign.

    Only untouched rows are cancelled. Anything already sent stays sent — the message
    is with the customer and pretending otherwise would make the counters lie.
    """
    campaign = _owned(db, campaign_id, user)
    if campaign.status not in ("sending", "ready", "screening", "draft"):
        raise HTTPException(409, f"Cannot cancel a campaign that is '{campaign.status}'")

    remaining = (
        db.query(BroadcastRecipient)
        .filter(
            BroadcastRecipient.campaign_id == campaign.id,
            BroadcastRecipient.status.in_(("draft", "pending")),
        )
        .update({"status": "skipped", "skip_reason": "cancelled"}, synchronize_session=False)
    )
    campaign.status = "cancelled"
    db.commit()
    return {"campaign_id": str(campaign.id), "cancelled": remaining}


# ── Progress and insights ─────────────────────────────────────────────────────

@router.get("/{campaign_id}/progress")
def campaign_progress(
    campaign_id: uuid.UUID,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Counters for the progress bar, polled while a campaign runs.

    `dispatched` counts send *attempts* — that is what the bar measures. Delivery is
    separate and keeps moving after the bar completes, because Meta reports failures
    asynchronously.
    """
    campaign = _owned(db, campaign_id, user)
    attempted = (campaign.sent or 0) + (campaign.failed or 0)
    sendable  = max((campaign.total or 0) - (campaign.skipped or 0), 0)
    return {
        "status":     campaign.status,
        "total":      campaign.total or 0,
        "sendable":   sendable,
        "attempted":  attempted,
        "sent":       campaign.sent or 0,
        "failed":     campaign.failed or 0,
        "skipped":    campaign.skipped or 0,
        "delivered":  campaign.delivered or 0,
        "read":       campaign.read or 0,
        "percent":    round(attempted / sendable * 100, 1) if sendable else 0.0,
        "is_running": campaign.status == "sending",
        # True while receipts may still change the numbers below the bar.
        "settling":   campaign.status == "dispatched",
    }


@router.get("/{campaign_id}/insights")
def campaign_insights(
    campaign_id: uuid.UUID,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Failure breakdown by error code, plus the rows behind it."""
    campaign = _owned(db, campaign_id, user)

    failures = (
        db.query(BroadcastRecipient)
        .filter(
            BroadcastRecipient.campaign_id == campaign.id,
            BroadcastRecipient.status == "failed",
        )
        .all()
    )
    by_code: dict[str, dict] = {}
    for r in failures:
        code = r.error_code or "unknown"
        entry = by_code.setdefault(code, {"code": code, "count": 0, "message": r.error_message})
        entry["count"] += 1

    skipped = (
        db.query(BroadcastRecipient)
        .filter(
            BroadcastRecipient.campaign_id == campaign.id,
            BroadcastRecipient.status == "skipped",
        )
        .all()
    )
    by_skip: dict[str, int] = {}
    for r in skipped:
        by_skip[r.skip_reason or "unknown"] = by_skip.get(r.skip_reason or "unknown", 0) + 1

    return {
        "status":     campaign.status,
        "sent":       campaign.sent or 0,
        "delivered":  campaign.delivered or 0,
        "read":       campaign.read or 0,
        "failed":     campaign.failed or 0,
        "skipped":    campaign.skipped or 0,
        "failures_by_code": sorted(by_code.values(), key=lambda e: -e["count"]),
        "skips_by_reason":  by_skip,
        "failed_rows": [
            {
                "mobile_no":     r.mobile_no,
                "customer_name": r.customer_name,
                "error_code":    r.error_code,
                "error_message": r.error_message,
            }
            for r in failures[:200]
        ],
    }


# ── Bulk delete ───────────────────────────────────────────────────────────────

class BulkDeleteRequest(BaseModel):
    ids: list[uuid.UUID]


class BulkDeleteResult(BaseModel):
    deleted: int
    failed:  list[str]
    # Ids refused because the campaign had already started sending, reported
    # separately from ids the user simply could not reach.
    in_flight: list[str] = []


@router.post("/bulk-delete", response_model=BulkDeleteResult)
def bulk_delete_campaigns(
    payload: BulkDeleteRequest,
    _perm=Depends(require("campaigns", "delete")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Delete several campaigns at once.

    Ownership is re-checked per id rather than trusting the list the browser sent —
    otherwise a company-scoped user could post another company's ids. One unreachable
    id does not abort the rest; it is reported instead.

    A campaign that has started sending is never deleted: it is the record of messages
    that really reached customers, and removing it would erase the only evidence of
    what was sent to whom.
    """
    if not payload.ids:
        return BulkDeleteResult(deleted=0, failed=[], in_flight=[])
    if len(payload.ids) > 200:
        raise HTTPException(413, "Too many campaigns in one request")

    deleted, failed, in_flight = 0, [], []
    for campaign_id in payload.ids:
        try:
            campaign = _owned(db, campaign_id, user)
        except HTTPException:
            failed.append(str(campaign_id))
            continue
        if campaign.status in ("sending", "dispatched", "settled"):
            in_flight.append(campaign.name)
            continue
        db.delete(campaign)        # recipients cascade
        deleted += 1

    db.commit()
    return BulkDeleteResult(deleted=deleted, failed=failed, in_flight=in_flight)


# ── CSV import (Personalized / param_mode = "per_row") ────────────────────────
#
# Mirrors the PhoneBook import trio deliberately: same three endpoints, same
# batching, same SAVEPOINT-per-row handling. The browser applies the column
# mapping and posts already-mapped rows, so nothing here parses CSV.

_MAX_IMPORT_BATCH = 500


class CampaignImportRow(BaseModel):
    """One CSV row after the browser has applied the column mapping."""
    row:           int                    # 1-based line number, for error reporting
    mobile_no:     str | None = None
    customer_name: str | None = None
    # {"param_1": "Ravi", "param_2": "10234", ...} — keyed as campaign_fields names
    params:        dict[str, str] = {}


class CampaignImportRequest(BaseModel):
    rows: list[CampaignImportRow]


class ImportError_(BaseModel):
    row:    int
    value:  str
    reason: str


class CampaignImportResult(BaseModel):
    imported: int
    skipped:  int
    total:    int              # recipients on the campaign after this batch
    errors:   list[ImportError_]


def _per_row_campaign(db: Session, campaign_id: uuid.UUID, user) -> BroadcastCampaign:
    """Fetch a campaign that is actually expecting a CSV, and still editable."""
    campaign = _owned(db, campaign_id, user)
    if campaign.param_mode != "per_row":
        raise HTTPException(409, "This campaign takes its recipients from PhoneBooks, not a CSV")
    if campaign.status not in ("draft", "screening", "ready"):
        raise HTTPException(409, f"Cannot import while campaign is '{campaign.status}'")
    return campaign


@router.get("/{campaign_id}/import/fields")
def campaign_import_fields(
    campaign_id: uuid.UUID,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Field catalogue for the mapping dialog.

    Campaign-scoped rather than static, because the number of parameter columns
    is whatever the chosen template declares.
    """
    campaign = _per_row_campaign(db, campaign_id, user)
    template = _template_for(db, campaign)
    n = template_body.param_count(template.components)
    return {"fields": field_catalogue(campaign_fields(n)), "param_count": n}


@router.post("/{campaign_id}/import/suggest-mapping")
def campaign_suggest_mapping(
    campaign_id: uuid.UUID,
    payload: dict,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    campaign = _per_row_campaign(db, campaign_id, user)
    template = _template_for(db, campaign)
    n = template_body.param_count(template.components)
    headers = payload.get("headers") or []
    return {"mapping": suggest_mapping(headers, campaign_fields(n))}


@router.post("/{campaign_id}/import", response_model=CampaignImportResult)
def campaign_import(
    campaign_id: uuid.UUID,
    payload: CampaignImportRequest,
    _perm=Depends(require("campaigns", "write")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Insert one batch of mapped rows as recipients.

    Each row goes in inside its own SAVEPOINT, so one bad row is skipped and
    reported rather than aborting the batch. Duplicates inside the same file are
    caught the same way — the second occurrence violates
    uq_broadcast_recipient_campaign_mobile.
    """
    campaign = _per_row_campaign(db, campaign_id, user)
    template = _template_for(db, campaign)
    n = template_body.param_count(template.components)

    if len(payload.rows) > _MAX_IMPORT_BATCH:
        raise HTTPException(413, f"Batch too large — send at most {_MAX_IMPORT_BATCH} rows")

    existing = db.query(BroadcastRecipient).filter(
        BroadcastRecipient.campaign_id == campaign.id
    ).count()
    if existing + len(payload.rows) > MAX_RECIPIENTS_PER_CAMPAIGN:
        raise HTTPException(
            400,
            f"That would take this broadcast past the current limit of "
            f"{MAX_RECIPIENTS_PER_CAMPAIGN} recipients",
        )

    imported = 0
    errors: list[ImportError_] = []

    for raw in payload.rows:
        raw_mobile = (raw.mobile_no or "").strip()

        try:
            mobile = _validate_mobile(raw_mobile)
        except ValueError as exc:
            errors.append(ImportError_(row=raw.row, value=raw_mobile, reason=str(exc)))
            continue

        # Every placeholder must have a value. A blank sends a real customer a
        # message with a hole in it, which is worse than a reported skip.
        ordered: list[str] = []
        missing: list[int] = []
        for i in range(1, n + 1):
            val = (raw.params.get(f"param_{i}") or "").strip()
            if not val:
                missing.append(i)
            ordered.append(val)
        if missing:
            nums = ", ".join(str(m) for m in missing)
            errors.append(ImportError_(
                row=raw.row, value=mobile,
                reason=f"Missing value for Param {nums}",
            ))
            continue

        try:
            with db.begin_nested():
                db.add(BroadcastRecipient(
                    campaign_id   = campaign.id,
                    mobile_no     = mobile,
                    customer_name = (raw.customer_name or "").strip() or None,
                    params        = ordered,
                    status        = "draft",
                ))
            imported += 1
        except IntegrityError:
            errors.append(ImportError_(
                row=raw.row, value=mobile,
                reason="Already in this broadcast",
            ))

    campaign.total = existing + imported
    campaign.status = "screening"

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        log_error("Campaign import batch failed",
                  f"POST /api/campaigns/{campaign_id}/import", exc, user=str(user.id))
        raise HTTPException(500, "Import failed — no rows in this batch were saved")

    return CampaignImportResult(
        imported=imported, skipped=len(errors),
        total=campaign.total, errors=errors,
    )
