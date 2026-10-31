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
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.deps import AgentContext, get_agent_session
from app.core.database import get_db

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
    books = db.execute(
        text("""
            SELECT p.id, p.name, p.updated_at
            FROM phonebooks p
            JOIN agent_phonebooks ap ON ap.phonebook_id = p.id
            WHERE ap.agent_id = :agent_id
              -- Defence in depth. The admin API refuses to map another company's book,
              -- but a user moved between companies must not keep reaching their old
              -- employer's contacts.
              AND p.company_id = :company_id
            ORDER BY p.name
        """),
        {"agent_id": ctx.agent_row_id, "company_id": ctx.company_id},
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
            # From the rows actually returned, so it can never disagree with the array
            # beside it the way a separately-counted total can.
            contact_count=len(by_book[str(b.id)]),
            updated_at=b.updated_at,
            contacts=by_book[str(b.id)],
        )
        for b in books
    ]
