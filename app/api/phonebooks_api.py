"""
/api/phonebooks — CRUD for phonebooks and their contacts.

Every route is guarded by require("phonebooks", ...) at router or route level,
and every query is company-scoped with company_filter(). A user with a
company_id must never see or touch another company's contact lists.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.column_mapper import field_catalogue, suggest_mapping
from app.core.database import get_db
from app.core.deps import company_filter, get_current_user, require
from app.models.phonebook import Phonebook, PhonebookContact
from app.schemas.phonebook import (
    ContactCreate,
    ContactOut,
    ContactUpdate,
    PhonebookCreate,
    PhonebookOut,
    PhonebookUpdate,
)
from app.utils.error_logger import log_error

# Rows are sent in batches by the browser so the progress bar reflects real
# work. Capped so one request can never hold a huge transaction open.
_MAX_IMPORT_BATCH = 500

router = APIRouter(
    prefix="/api/phonebooks",
    tags=["PhoneBooks"],
    dependencies=[Depends(require("phonebooks", "read"))],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_owned_phonebook(db: Session, list_id: uuid.UUID, user) -> Phonebook:
    """Fetch a phonebook, 404 if absent and 403 if it belongs to another company."""
    pb = db.query(Phonebook).filter(Phonebook.id == list_id).first()
    if not pb:
        raise HTTPException(404, "PhoneBook not found")

    cid = company_filter(user)
    if cid and str(pb.company_id) != cid:
        raise HTTPException(403, "Access denied")
    return pb


def _get_owned_contact(db: Session, contact_id: uuid.UUID, user) -> PhonebookContact:
    contact = db.query(PhonebookContact).filter(PhonebookContact.id == contact_id).first()
    if not contact:
        raise HTTPException(404, "Contact not found")
    # Ownership is resolved through the parent phonebook.
    _get_owned_phonebook(db, contact.phonebook_id, user)
    return contact


def _counts(db: Session, phonebook_ids: list[uuid.UUID]) -> dict:
    """Contact counts for many phonebooks in one query — never per row."""
    if not phonebook_ids:
        return {}
    rows = (
        db.query(PhonebookContact.phonebook_id, func.count(PhonebookContact.id))
        .filter(PhonebookContact.phonebook_id.in_(phonebook_ids))
        .group_by(PhonebookContact.phonebook_id)
        .all()
    )
    return {pid: n for pid, n in rows}


# ── PhoneBooks ────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[PhonebookOut])
def list_phonebooks(user=Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(Phonebook)
    cid = company_filter(user)
    if cid:
        q = q.filter(Phonebook.company_id == uuid.UUID(cid))

    books = q.order_by(Phonebook.created_at.desc()).all()
    counts = _counts(db, [b.id for b in books])
    return [
        PhonebookOut(
            id=b.id, company_id=b.company_id, name=b.name,
            created_at=b.created_at, contact_count=counts.get(b.id, 0),
        )
        for b in books
    ]


@router.post("/", response_model=PhonebookOut, status_code=201)
def create_phonebook(
    payload: PhonebookCreate,
    request: Request,
    _perm=Depends(require("phonebooks", "create")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cid = company_filter(user)
    if cid:
        # Company-scoped users always create under their own company, whatever
        # the payload claims.
        company_id = uuid.UUID(cid)
    else:
        if not payload.company_id:
            raise HTTPException(400, "company_id is required")
        company_id = payload.company_id

    pb = Phonebook(
        company_id=company_id,
        name=payload.name,
        created_by_id=uuid.UUID(str(user.id)),
    )
    db.add(pb)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"A PhoneBook named '{payload.name}' already exists for this company")
    except Exception as exc:
        db.rollback()
        log_error("PhoneBook create failed", "POST /api/phonebooks/", exc,
                  request=request, user=str(user.id))
        raise HTTPException(500, "Internal error")

    db.refresh(pb)
    return PhonebookOut(id=pb.id, company_id=pb.company_id, name=pb.name,
                            created_at=pb.created_at, contact_count=0)


@router.get("/{phonebook_id}", response_model=PhonebookOut)
def get_phonebook(
    phonebook_id: uuid.UUID,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pb = _get_owned_phonebook(db, phonebook_id, user)
    n = db.query(func.count(PhonebookContact.id)).filter(
        PhonebookContact.phonebook_id == pb.id
    ).scalar() or 0
    return PhonebookOut(id=pb.id, company_id=pb.company_id, name=pb.name,
                            created_at=pb.created_at, contact_count=n)


@router.put("/{phonebook_id}", response_model=PhonebookOut)
def update_phonebook(
    phonebook_id: uuid.UUID,
    payload: PhonebookUpdate,
    _perm=Depends(require("phonebooks", "write")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pb = _get_owned_phonebook(db, phonebook_id, user)
    if payload.name is not None:
        pb.name = payload.name
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"A PhoneBook named '{payload.name}' already exists for this company")

    db.refresh(pb)
    n = db.query(func.count(PhonebookContact.id)).filter(
        PhonebookContact.phonebook_id == pb.id
    ).scalar() or 0
    return PhonebookOut(id=pb.id, company_id=pb.company_id, name=pb.name,
                            created_at=pb.created_at, contact_count=n)


@router.delete("/{phonebook_id}", status_code=204)
def delete_phonebook(
    phonebook_id: uuid.UUID,
    _perm=Depends(require("phonebooks", "delete")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pb = _get_owned_phonebook(db, phonebook_id, user)
    # Contacts cascade. Campaigns snapshot their recipients, so deleting a
    # phonebook never alters a campaign that has already run.
    db.delete(pb)
    db.commit()


# ── Contacts ──────────────────────────────────────────────────────────────────

@router.post("/{phonebook_id}/contacts", response_model=ContactOut, status_code=201)
def add_contact(
    phonebook_id: uuid.UUID,
    payload: ContactCreate,
    _perm=Depends(require("phonebooks", "write")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pb = _get_owned_phonebook(db, phonebook_id, user)

    contact = PhonebookContact(
        phonebook_id=pb.id,
        mobile_no=payload.mobile_no,
        customer_name=payload.customer_name,
        email=payload.email,
        agent_id=payload.agent_id,
    )
    db.add(contact)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"{payload.mobile_no} is already in this phonebook")

    db.refresh(contact)
    return contact


@router.put("/contacts/{contact_id}", response_model=ContactOut)
def update_contact(
    contact_id: uuid.UUID,
    payload: ContactUpdate,
    _perm=Depends(require("phonebooks", "write")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    contact = _get_owned_contact(db, contact_id, user)

    # Only touch fields the request actually sent, so a PUT carrying a subset
    # cannot blank the rest. Within those, an explicit null means "clear it" —
    # required fields reject null in the schema, so only email/agent_id can.
    sent = payload.model_fields_set
    for field in ("mobile_no", "customer_name", "email", "agent_id"):
        if field not in sent:
            continue
        value = getattr(payload, field)
        if value is None and field in ("mobile_no", "customer_name"):
            continue          # schema already rejects blanks; null = "unchanged"
        setattr(contact, field, value)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"{payload.mobile_no} is already in this phonebook")

    db.refresh(contact)
    return contact


@router.delete("/contacts/{contact_id}", status_code=204)
def delete_contact(
    contact_id: uuid.UUID,
    _perm=Depends(require("phonebooks", "delete")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    contact = _get_owned_contact(db, contact_id, user)
    db.delete(contact)
    db.commit()


# ── CSV import ────────────────────────────────────────────────────────────────

class MappingSuggestRequest(BaseModel):
    headers: list[str]


class ImportRow(BaseModel):
    """One CSV row after the browser has applied the column mapping."""
    row:           int                     # 1-based line number, for error reporting
    mobile_no:     str | None = None
    customer_name: str | None = None
    email:         str | None = None
    agent_id:      str | None = None


class ImportBatchRequest(BaseModel):
    rows: list[ImportRow]


class ImportError_(BaseModel):
    row:    int
    value:  str
    reason: str


class ImportBatchResult(BaseModel):
    imported: int
    skipped:  int
    errors:   list[ImportError_]


@router.get("/import/fields")
def import_fields(_user=Depends(get_current_user)):
    """Field catalogue for the mapping dialog. No phonebook needed."""
    return {"fields": field_catalogue()}


@router.post("/import/suggest-mapping")
def import_suggest_mapping(
    payload: MappingSuggestRequest,
    _user=Depends(get_current_user),
):
    """
    Guess which CSV column feeds each field.

    Deliberately guesses nothing when a header is too short or unlike anything
    we know — an empty dropdown is safer than a confident wrong pre-selection
    the user does not think to check.
    """
    return {"mapping": suggest_mapping(payload.headers)}


@router.post("/{phonebook_id}/import", response_model=ImportBatchResult)
def import_contacts(
    phonebook_id: uuid.UUID,
    payload: ImportBatchRequest,
    _perm=Depends(require("phonebooks", "write")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Insert one batch of mapped rows.

    Each row is inserted inside its own SAVEPOINT, so a single bad row is
    skipped and reported rather than aborting the batch — which is what
    "skip the two rows as error and show on final error insights" requires.
    Duplicates inside the same file are caught the same way: the second
    occurrence violates uq_phonebook_contact_mobile and is reported.
    """
    pb = _get_owned_phonebook(db, phonebook_id, user)

    if len(payload.rows) > _MAX_IMPORT_BATCH:
        raise HTTPException(413, f"Batch too large — send at most {_MAX_IMPORT_BATCH} rows")

    imported = 0
    errors: list[ImportError_] = []

    for raw in payload.rows:
        # Validate through the same schema the manual add form uses, so the CSV
        # path cannot accept anything the UI would reject.
        try:
            contact_in = ContactCreate(
                mobile_no=(raw.mobile_no or "").strip(),
                customer_name=(raw.customer_name or "").strip(),
                email=raw.email,
                agent_id=raw.agent_id,
            )
        except Exception as exc:
            errors.append(ImportError_(
                row=raw.row,
                value=(raw.mobile_no or raw.customer_name or "").strip(),
                reason=_first_message(exc),
            ))
            continue

        try:
            with db.begin_nested():
                db.add(PhonebookContact(
                    phonebook_id=pb.id,
                    mobile_no=contact_in.mobile_no,
                    customer_name=contact_in.customer_name,
                    email=contact_in.email,
                    agent_id=contact_in.agent_id,
                ))
            imported += 1
        except IntegrityError:
            errors.append(ImportError_(
                row=raw.row,
                value=contact_in.mobile_no,
                reason="Already in this phonebook",
            ))

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        log_error("PhoneBook import batch failed", f"POST /api/phonebooks/{phonebook_id}/import",
                  exc, user=str(user.id))
        raise HTTPException(500, "Import failed — no rows in this batch were saved")

    return ImportBatchResult(imported=imported, skipped=len(errors), errors=errors)


def _first_message(exc: Exception) -> str:
    """Pull a readable sentence out of a Pydantic ValidationError."""
    errs = getattr(exc, "errors", None)
    if callable(errs):
        try:
            msg = errs()[0].get("msg", "")
            return msg.replace("Value error, ", "") or "Invalid row"
        except Exception:
            pass
    return str(exc) or "Invalid row"


# ── Bulk delete ───────────────────────────────────────────────────────────────
# Path is a single literal segment, so it cannot collide with /{phonebook_id};
# that route is DELETE, this one is POST.

class BulkDeleteRequest(BaseModel):
    ids: list[uuid.UUID]


class BulkDeleteResult(BaseModel):
    deleted: int
    failed:  list[str]


@router.post("/bulk-delete", response_model=BulkDeleteResult)
def bulk_delete_phonebooks(
    payload: BulkDeleteRequest,
    _perm=Depends(require("phonebooks", "delete")),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Delete several PhoneBooks at once.

    Ownership is re-checked per id rather than trusting the list the browser
    sent — otherwise a company-scoped user could post someone else's ids.
    One unreachable id does not abort the rest; it is reported instead.
    """
    if not payload.ids:
        return BulkDeleteResult(deleted=0, failed=[])
    if len(payload.ids) > 200:
        raise HTTPException(413, "Too many PhoneBooks in one request")

    deleted, failed = 0, []
    for list_id in payload.ids:
        try:
            pb = _get_owned_phonebook(db, list_id, user)
        except HTTPException:
            failed.append(str(list_id))
            continue
        db.delete(pb)          # contacts cascade
        deleted += 1

    db.commit()
    return BulkDeleteResult(deleted=deleted, failed=failed)
