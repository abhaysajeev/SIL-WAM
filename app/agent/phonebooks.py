"""
The phonebooks an agent may work from, with their contacts.

Returned fully nested in one call: the app holds the whole set so it can browse and
search offline. A phonebook tops out at ~2,000 contacts by design, so the worst case is
a few hundred kilobytes per book — fine for an occasional sync, not something to call on
every screen.

Two things here are load-bearing and easy to undo by accident:

**`opted_out` is joined live, never stored.** A contact who texted STOP is a hard block
in the broadcast pipeline (BROADCAST_DESIGN.md §9), but nothing on the phonebook side has
ever surfaced that — so without this the agent app would be the one place in the system
that cheerfully messages people who unsubscribed. The design is explicit that this must
be a join at display time: a denormalised copy on the contact goes stale the moment
somebody texts STOP, which is exactly the failure the rule exists to prevent.

**Contacts are not filtered by the caller's `agent_id`.** Each contact carries its own,
which may name a different agent entirely. That is a separate feature; here the whole
book is returned and every row keeps its own value.
"""
import uuid as _uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent import errors
from app.agent.deps import AgentContext, get_agent_session
from app.core import broadcast_config, phone
from app.api.phonebooks_api import _MAX_IMPORT_BATCH, _first_message
from app.core.database import get_db
from app.models.agent import AgentPhonebook
from app.models.phonebook import Phonebook, PhonebookContact
from app.services import contact_import
from app.schemas.phonebook import ContactCreate, ContactUpdate

router = APIRouter(prefix="/phonebooks", tags=["Agent App"])


class ContactOut(BaseModel):
    id:            str
    mobile_no:     str
    customer_name: str
    email:         str | None
    # This contact's own agent, as imported. May differ from the caller's — see the
    # module docstring.
    agent_id:      str | None
    # True when this number has unsubscribed from this company. The app must not offer
    # to message them.
    opted_out:     bool


class PhonebookOut(BaseModel):
    id:            str
    name:          str
    # True for a book this agent created from the app: they may rename it and add,
    # edit or remove its contacts. Books an administrator mapped to them are read-only.
    editable:      bool
    contact_count: int
    updated_at:    datetime | None
    contacts:      list[ContactOut]


@router.get("", response_model=list[PhonebookOut])
def list_agent_phonebooks(
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    """
    Every phonebook mapped to this agent, with all of its contacts.

    An empty list means none have been assigned yet — not a failure. As with templates,
    it deliberately does not fall back to "all of the company's phonebooks": an agent
    gets what somebody chose to give them.

    Two queries regardless of how many books or contacts are involved. Fetching contacts
    per phonebook would be N+1 on the one endpoint most likely to grow.
    """
    return _load_books(db, ctx)


def _load_books(db: Session, ctx: AgentContext, only_id: str | None = None) -> list[PhonebookOut]:
    """The caller's mapped books with nested contacts; one book when `only_id` is given."""
    one_book = "AND p.id = CAST(:only_id AS uuid)" if only_id else ""
    books = db.execute(
        text(f"""
            SELECT p.id, p.name, p.updated_at,
                   (p.created_by_id = CAST(:user_id AS uuid)) AS editable
            FROM phonebooks p
            JOIN agent_phonebooks ap ON ap.phonebook_id = p.id
            WHERE ap.agent_id = :agent_id
              -- Defence in depth. The admin API refuses to map another company's book,
              -- but a user moved between companies must not keep reaching their old
              -- employer's contacts.
              AND p.company_id = :company_id
              {one_book}
            ORDER BY p.name
        """),
        {"agent_id": ctx.agent_row_id, "company_id": ctx.company_id,
         "user_id": ctx.user_id, "only_id": only_id},
    ).fetchall()

    if not books:
        return []

    book_ids = [str(b.id) for b in books]

    rows = db.execute(
        text("""
            SELECT pc.id, pc.phonebook_id, pc.mobile_no, pc.customer_name,
                   pc.email, pc.agent_id,
                   (mo.mobile_no IS NOT NULL) AS opted_out
            FROM phonebook_contacts pc
            -- Opt-out is company-scoped: unsubscribing from one business must not
            -- unsubscribe you from another.
            LEFT JOIN messaging_optouts mo
                   ON mo.mobile_no  = pc.mobile_no
                  AND mo.company_id = :company_id
            -- Cast explicitly: a Python list of strings binds as text[], and
            -- `uuid = text` has no operator in Postgres.
            WHERE pc.phonebook_id = ANY(CAST(:book_ids AS uuid[]))
            ORDER BY pc.customer_name, pc.mobile_no
        """),
        {"company_id": ctx.company_id, "book_ids": book_ids},
    ).fetchall()

    by_book: dict[str, list[ContactOut]] = {bid: [] for bid in book_ids}
    for r in rows:
        by_book[str(r.phonebook_id)].append(ContactOut(
            id=str(r.id),
            mobile_no=r.mobile_no,
            customer_name=r.customer_name,
            email=r.email,
            agent_id=r.agent_id,
            opted_out=bool(r.opted_out),
        ))

    return [
        PhonebookOut(
            id=str(b.id),
            name=b.name,
            editable=bool(b.editable),
            # From the rows actually returned, so it can never disagree with the array
            # beside it the way a separately-counted total can.
            contact_count=len(by_book[str(b.id)]),
            updated_at=b.updated_at,
            contacts=by_book[str(b.id)],
        )
        for b in books
    ]


# ── Phonebooks the agent builds ────────────────────────────────────────────
#
# An agent may create a phonebook under their own company, and may rename it and add,
# edit or remove its contacts. Nothing else: books an administrator mapped to them stay
# read-only, and deleting a whole book is left to administrators.
#
# Contacts go through the same ContactCreate validation as the web form and CSV import,
# so the app can never store a number the rest of the system would refuse. Opt-outs are
# not touched here: they are joined live wherever a contact is read or sent to, so an
# agent adding a number that texted STOP still cannot message it.

# The GET endpoint returns every contact of every book in one response and was sized for
# books of about this many; an agent-built book is held to the same.
MAX_CONTACTS_PER_BOOK = 2000      # default; the live value is the company's setting


def book_limit(db: Session, company_id) -> int:
    """Max contacts in one agent-built phonebook — Broadcast settings, per company."""
    return broadcast_config.get(db, company_id).agent_max_contacts_per_phonebook


class ContactIn(BaseModel):
    """One row as the app sends it. Validated per row, so one bad row is reported and
    skipped rather than failing the whole request."""
    mobile_no:     str | None = None
    customer_name: str | None = None
    email:         str | None = None


class PhonebookCreateIn(BaseModel):
    name:     str = Field(..., max_length=150)
    contacts: list[ContactIn] = Field(default_factory=list, max_length=_MAX_IMPORT_BATCH)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Phonebook name is required")
        return v


class PhonebookRenameIn(BaseModel):
    name: str = Field(..., max_length=150)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Phonebook name is required")
        return v


class ContactsAddIn(BaseModel):
    contacts: list[ContactIn] = Field(..., min_length=1, max_length=_MAX_IMPORT_BATCH)


class ContactEditIn(BaseModel):
    mobile_no:     str | None = None
    customer_name: str | None = None
    email:         str | None = None


class SkippedRow(BaseModel):
    row:    int          # 1-based position in the request's contacts array
    value:  str
    reason: str


class PhonebookCreated(PhonebookOut):
    skipped: list[SkippedRow]


class ContactsAdded(BaseModel):
    added:   int
    skipped: list[SkippedRow]


class ContactSaved(BaseModel):
    id:            str
    mobile_no:     str
    customer_name: str
    email:         str | None
    agent_id:      str | None


def _owned_book(db: Session, ctx: AgentContext, phonebook_id: _uuid.UUID) -> Phonebook:
    """
    The book, locked for this transaction, if the caller may change it.

    Not theirs to see (another company, not mapped to them, no such id) is a 404 that
    confirms nothing. Mapped to them but made by an administrator is a 403 that says so.
    The row lock keeps two concurrent adds from both passing the size check.
    """
    mapped = db.execute(
        text("""SELECT 1 FROM agent_phonebooks
                WHERE agent_id = :agent AND phonebook_id = :pid"""),
        {"agent": ctx.agent_row_id, "pid": str(phonebook_id)},
    ).first()
    book = (
        db.query(Phonebook)
        .filter(Phonebook.id == phonebook_id, Phonebook.company_id == _uuid.UUID(ctx.company_id))
        .with_for_update()
        .first()
    ) if mapped and ctx.company_id else None
    if book is None:
        raise errors.not_found("phonebook")
    if str(book.created_by_id) != ctx.user_id:
        raise errors.phonebook_read_only()
    return book


def _add_contacts(db: Session, ctx: AgentContext, book: Phonebook, rows: list[ContactIn],
                  row_numbers: list[int] | None = None):
    """Validate and insert rows; returns (added, skipped). Caller commits."""
    current = db.query(PhonebookContact).filter(PhonebookContact.phonebook_id == book.id).count()
    region = phone.company_region(db, book.company_id)
    valid: list[tuple[int, ContactCreate]] = []
    skipped: list[SkippedRow] = []
    for i, raw in enumerate(rows):
        # Position in the request, or the spreadsheet row for a file import — whichever
        # the person can find the bad line by.
        n = row_numbers[i] if row_numbers else i + 1
        try:
            # Canonical form first (918075571893), so the number a broadcast is sent to
            # is the number the customer's reply arrives from. See app/core/phone.py.
            canonical = phone.normalize(raw.mobile_no, region)
            valid.append((n, ContactCreate(
                mobile_no=canonical,
                customer_name=(raw.customer_name or "").strip(),
                email=raw.email,
                agent_id=ctx.agent_id,
            )))
        except Exception as exc:                 # noqa: BLE001 -- reported per row
            skipped.append(SkippedRow(row=n, value=(raw.mobile_no or raw.customer_name or "").strip(),
                                      reason=_first_message(exc)))

    limit = book_limit(db, book.company_id)
    if current + len(valid) > limit:
        raise errors.phonebook_full(current, len(valid), limit)

    added = 0
    for n, contact in valid:
        try:
            # One savepoint per row, as the web import does: a duplicate is skipped and
            # reported instead of rolling back the rows before it.
            with db.begin_nested():
                db.add(PhonebookContact(
                    phonebook_id=book.id,
                    mobile_no=contact.mobile_no,
                    customer_name=contact.customer_name,
                    email=contact.email,
                    agent_id=contact.agent_id,
                ))
            added += 1
        except IntegrityError:
            skipped.append(SkippedRow(row=n, value=contact.mobile_no, reason="Already in this phonebook"))
    skipped.sort(key=lambda s: s.row)
    return added, skipped


@router.post("", response_model=PhonebookCreated, status_code=201)
def create_phonebook(
    payload: PhonebookCreateIn,
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    """Create a phonebook under the agent's company, mapped to them, with optional contacts."""
    if not ctx.company_id:
        raise errors.no_company()

    book = Phonebook(company_id=_uuid.UUID(ctx.company_id), name=payload.name,
                     created_by_id=_uuid.UUID(ctx.user_id))
    try:
        with db.begin_nested():
            db.add(book)
            db.flush()
    except IntegrityError:
        db.rollback()
        raise errors.phonebook_name_taken(payload.name)

    db.add(AgentPhonebook(agent_id=_uuid.UUID(ctx.agent_row_id), phonebook_id=book.id))
    db.flush()
    _added, skipped = _add_contacts(db, ctx, book, payload.contacts)
    db.commit()

    [out] = _load_books(db, ctx, only_id=str(book.id))
    return PhonebookCreated(**out.model_dump(), skipped=skipped)


@router.patch("/{phonebook_id}", response_model=PhonebookOut)
def rename_phonebook(
    phonebook_id: _uuid.UUID,
    payload: PhonebookRenameIn,
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    book = _owned_book(db, ctx, phonebook_id)
    if book.name != payload.name:
        try:
            with db.begin_nested():
                book.name = payload.name
                db.flush()
        except IntegrityError:
            db.rollback()
            raise errors.phonebook_name_taken(payload.name)
        db.commit()
    [out] = _load_books(db, ctx, only_id=str(phonebook_id))
    return out


@router.post("/{phonebook_id}/contacts", response_model=ContactsAdded, status_code=201)
def add_contacts(
    phonebook_id: _uuid.UUID,
    payload: ContactsAddIn,
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    book = _owned_book(db, ctx, phonebook_id)
    added, skipped = _add_contacts(db, ctx, book, payload.contacts)
    book.updated_at = func.now()
    db.commit()
    return ContactsAdded(added=added, skipped=skipped)


class ImportResult(BaseModel):
    added:         int
    # Row numbers are the spreadsheet's own (row 1 = the headings).
    skipped:       list[SkippedRow]
    total_in_book: int
    # Which column in the file was read as which field.
    columns:       dict[str, str | None]


@router.get("/sample")
def phonebook_sample(
    format: str = Query("xlsx", pattern="^(xlsx|csv)$"),
    ctx: AgentContext = Depends(get_agent_session),
):
    """A blank contact file to fill in: headings only, number column formatted as Text."""
    content, mime, filename = contact_import.sample(format)
    return Response(content=content, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Cache-Control": "no-store"})


@router.post("/{phonebook_id}/import", response_model=ImportResult, status_code=201)
async def import_contacts(
    phonebook_id: _uuid.UUID,
    file: UploadFile = File(...),
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    """
    Add every contact in an Excel (.xlsx) or CSV file to a phonebook this agent created.

    Same per-row rules as POST /{id}/contacts: a bad number or a duplicate is skipped
    and reported with its spreadsheet row, the rest are added. The file as a whole is
    refused only if it cannot be read, has no number column, or would take the book
    past the company's limit — in which case nothing is added.
    """
    data = await file.read(contact_import.MAX_FILE_BYTES + 1)
    try:
        rows, mapping = contact_import.read_contacts(data, file.filename)
    except contact_import.ImportError_ as exc:
        raise errors.media(exc.code, exc.message)

    book = _owned_book(db, ctx, phonebook_id)
    limit = book_limit(db, book.company_id)
    if len(rows) > limit:
        raise errors.phonebook_full(0, len(rows), limit)

    contacts = [ContactIn(mobile_no=r.mobile_no, customer_name=r.customer_name,
                          email=r.email) for r in rows]
    added, skipped = _add_contacts(db, ctx, book, contacts,
                                   row_numbers=[r.row for r in rows])
    book.updated_at = func.now()
    db.commit()
    total = db.query(PhonebookContact).filter(
        PhonebookContact.phonebook_id == book.id).count()
    return ImportResult(added=added, skipped=skipped, total_in_book=total, columns=mapping)


def _owned_contact(db: Session, ctx: AgentContext, phonebook_id, contact_id) -> PhonebookContact:
    _owned_book(db, ctx, phonebook_id)
    contact = db.query(PhonebookContact).filter(
        PhonebookContact.id == contact_id, PhonebookContact.phonebook_id == phonebook_id,
    ).first()
    if contact is None:
        raise errors.not_found("contact")
    return contact


@router.patch("/{phonebook_id}/contacts/{contact_id}", response_model=ContactSaved)
def edit_contact(
    phonebook_id: _uuid.UUID,
    contact_id: _uuid.UUID,
    payload: ContactEditIn,
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    contact = _owned_contact(db, ctx, phonebook_id, contact_id)
    data = payload.model_dump(exclude_unset=True)
    try:
        if data.get("mobile_no") is not None:
            data["mobile_no"] = phone.normalize_for_company(
                db, ctx.company_id, data["mobile_no"])
        changes = ContactUpdate(**data)
    except Exception as exc:                     # noqa: BLE001
        raise errors.invalid_contact(_first_message(exc))

    for field in ("mobile_no", "customer_name"):
        value = getattr(changes, field)
        if value is not None:
            setattr(contact, field, value)
    if "email" in payload.model_fields_set:
        contact.email = changes.email
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        db.rollback()
        raise errors.contact_exists(changes.mobile_no or contact.mobile_no)
    db.commit()
    db.refresh(contact)
    return ContactSaved(id=str(contact.id), mobile_no=contact.mobile_no,
                        customer_name=contact.customer_name, email=contact.email,
                        agent_id=contact.agent_id)


@router.delete("/{phonebook_id}/contacts/{contact_id}", status_code=204)
def remove_contact(
    phonebook_id: _uuid.UUID,
    contact_id: _uuid.UUID,
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    contact = _owned_contact(db, ctx, phonebook_id, contact_id)
    db.delete(contact)
    db.commit()
    return Response(status_code=204)
