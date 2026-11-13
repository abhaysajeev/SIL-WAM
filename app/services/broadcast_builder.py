"""
Turning a set of phonebooks into a campaign's recipient rows.

Extracted from `campaigns_api.build_recipients` so the agent app can start a broadcast
through **exactly** the same code rather than a parallel implementation. That matters
more than the deduplication: the dedup rule, the mobile-number validation, the
per-campaign ceiling and the "rebuilding replaces" semantics are all decisions that
would drift the moment there were two copies, and the copy the handset used would be
the one nobody looked at.

Raises `BuildError` rather than `HTTPException`: this layer has no opinion about how a
failure should be shaped, and the two callers shape them differently — the web panel
returns FastAPI's `{"detail": "..."}`, the agent API returns its own
`{"detail": {"code": ..., "message": ...}}` taxonomy.
"""
import logging

from sqlalchemy.orm import Session

from app.core import broadcast_config, phone
from app.models.broadcast_campaign import (
    MAX_RECIPIENTS_PER_CAMPAIGN,
    BroadcastRecipient,
)
from app.models.phonebook import Phonebook, PhonebookContact
from app.schemas.phonebook import _validate_mobile

logger = logging.getLogger(__name__)


class BuildError(Exception):
    """A recipient build that cannot proceed. `code` is the HTTP status to map to."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def build_from_phonebooks(
    db: Session,
    campaign,
    template,
    phonebook_ids: list,
    params: list[str],
    header_media: dict | None = None,
    stop_on_error: bool = False,
    contact_ids: list | None = None,
) -> dict:
    """
    Replace this campaign's recipients with the contacts of the given phonebooks.

    Does not commit — the caller owns the transaction, so a campaign that fails
    validation half way is never left with a partial list.

    `contact_ids` narrows the build to a subset of those phonebooks' contacts — the
    agent app ticking rows in a list. Its three states are deliberately distinct:

        None  → every contact in the books. The web panel always passes this, so the
                admin path is exactly what it was before selection existed.
        [...] → only these contacts.
        []    → refused. An empty list is never "no filter": that reading turns an app
                bug which serialises an empty array into a broadcast to the entire
                phonebook, which is the one mistake here nobody can take back.

    Callers are expected to have checked the ids belong to the books already, and to
    report their own error for ones that do not — an id that survives to here but
    matches nothing is simply not selected.
    """
    if contact_ids is not None and not contact_ids:
        raise BuildError(400, "No contacts were selected")
    lists = db.query(Phonebook).filter(
        Phonebook.id.in_(phonebook_ids),
        # Scoped to the campaign's company, not the caller's: a phonebook id from
        # another company must not resolve however the request arrived.
        Phonebook.company_id == campaign.company_id,
    ).all()
    if len(lists) != len(set(phonebook_ids)):
        raise BuildError(404, "One or more phonebooks were not found for this company")

    contact_q = db.query(PhonebookContact).filter(
        PhonebookContact.phonebook_id.in_([l.id for l in lists])
    )
    if contact_ids is not None:
        # Intersected with the phonebook filter above rather than replacing it, so an
        # id from a book the caller was not given cannot pull in a contact.
        contact_q = contact_q.filter(PhonebookContact.id.in_(contact_ids))
    contacts = contact_q.all()

    # Dedup across lists — the same number in two lists is one recipient. Meta bills
    # unique recipients, and being messaged twice by one campaign reads as a bug.
    # First occurrence wins, which also decides whose agent_id is snapshotted.
    #
    # Numbers are put in Meta's canonical form (918075571893) first, so `8075571893` and
    # `+91 80755 71893` in two books are one recipient, and the chat thread this send
    # writes is the same one the customer's reply will land in. A number the phone
    # library cannot read keeps the old behaviour (7–15 digits, '+' stripped) rather
    # than being dropped: it was sendable before this existed and must stay sendable.
    region = broadcast_config.get(db, campaign.company_id).default_country
    seen: dict[str, PhonebookContact] = {}
    for c in contacts:
        mobile = phone.try_normalize(c.mobile_no, region)
        if mobile is None:
            try:
                mobile = _validate_mobile(c.mobile_no)
            except ValueError:
                continue          # unusable number — silently excluded, never sent
        seen.setdefault(mobile, c)

    if len(seen) > MAX_RECIPIENTS_PER_CAMPAIGN:
        raise BuildError(
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
            params        = list(params),
            status        = "draft",
        ))

    campaign.shared_params     = list(params)
    campaign.header_media      = header_media
    campaign.header_media_mode = "shared" if header_media else None
    campaign.stop_on_error     = stop_on_error
    campaign.total             = len(seen)
    campaign.status            = "screening"
    campaign.skipped           = 0

    return {
        "campaign_id": str(campaign.id),
        "total": len(seen),
        "duplicates_removed": len(contacts) - len(seen),
    }
