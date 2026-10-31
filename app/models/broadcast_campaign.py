"""
Broadcast campaigns and their recipients.

    companies
     └─< broadcast_campaigns          (one template, one parameter mode)
          └─< broadcast_recipients    (one row per person, unique per campaign)

A campaign snapshots everything it needs onto its recipient rows at build time —
mobile number, customer name, agent_id and the resolved template parameters. Editing
or deleting a phonebook afterwards never rewrites the history of a campaign that
already ran.

Deliberately no Conversation or Message rows: a 1,000-recipient campaign would swamp
the transactional tables and the Conversations list, which is ordered by last activity.
The consequence is that delivery receipts cannot be matched through Message.wamid, so
`wamid` is stored here and conversation_engine.handle_status falls back to it — see
docs/BROADCAST_DESIGN.md §8, which is load-bearing.
"""
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.core.database import Base

# Raised deliberately low for the first release. The pipeline is not the limit —
# Meta's per-24h tier is — but a small cap keeps the blast radius small while the
# send path is still new.
MAX_RECIPIENTS_PER_CAMPAIGN = 1000


class BroadcastCampaign(Base):
    """One send of one template to many people."""
    __tablename__ = "broadcast_campaigns"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    name       = Column(String(200), nullable=False)
    # SET NULL so deleting a template never destroys the record of what was sent —
    # the recipients keep their resolved params regardless.
    template_id = Column(UUID(as_uuid=True),
                         ForeignKey("whatsapp_templates.id", ondelete="SET NULL"),
                         nullable=True)

    # "same"    — one set of parameters for everyone (Case 1)
    # "per_row" — parameters supplied per recipient via CSV (Case 2, not yet built)
    param_mode    = Column(String(20), nullable=False, default="same")
    shared_params = Column(JSONB, nullable=True)   # Case 1 values, ordered by {{n}}

    # The file that fills an IMAGE/DOCUMENT/VIDEO template header — one file for the
    # whole campaign. NULL for templates with a text header or none at all, and NULL
    # in per-row mode, where each recipient carries its own image instead.
    #
    #   {"format": "IMAGE", "source": "upload"|"link",
    #    "media_id": "...", "source_url": "https://..." | None,
    #    "filename": "...", "mime": "image/jpeg", "attached_at": "<iso8601>"}
    #
    # Always a media_id: a pasted link is downloaded once at attach time and uploaded
    # to Meta like any other file, so every send addresses the same id. Handing Meta
    # the URL instead would have it fetch that address once per recipient. `source`
    # and `source_url` are provenance only — never used to address a send.
    header_media  = Column(JSONB, nullable=True)

    # How a media-header template gets its file:
    #   "shared"  — one file for everyone, on header_media above
    #   "per_row" — each recipient's own URL, on broadcast_recipients.header_media_url
    # NULL means the template has no media header at all. A column rather than
    # something inferred from header_media being NULL, because "per-row mode" and
    # "no media needed" are different things that must not look alike.
    header_media_mode = Column(String(20), nullable=True)

    # draft → screening → ready → sending → dispatched → settled
    # "dispatched" means every send was attempted; "settled" means the receipts are in.
    # They are different states because Meta reports failures asynchronously.
    status = Column(String(20), nullable=False, default="draft", index=True)

    # Halt the run on the first failure instead of pushing through. Off by default:
    # one bad number should not stop a campaign.
    stop_on_error = Column(Boolean, nullable=False, default=False, server_default="false")

    # Denormalised counters — the progress bar and insights read these rather than
    # aggregating 1,000 rows on every poll.
    total     = Column(Integer, nullable=False, default=0)
    sent      = Column(Integer, nullable=False, default=0)
    delivered = Column(Integer, nullable=False, default=0)
    read      = Column(Integer, nullable=False, default=0)
    failed    = Column(Integer, nullable=False, default=0)
    skipped   = Column(Integer, nullable=False, default=0)

    created_by_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
                           nullable=True)
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    settled_at    = Column(DateTime(timezone=True), nullable=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
    updated_at    = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BroadcastRecipient(Base):
    """One person in one campaign, with their resolved parameters."""
    __tablename__ = "broadcast_recipients"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id = Column(UUID(as_uuid=True),
                         ForeignKey("broadcast_campaigns.id", ondelete="CASCADE"),
                         nullable=False)

    # Normalised by _validate_mobile — digits only, no leading '+'. Meta's webhooks
    # never include a '+', so anything else breaks matching on the way back.
    mobile_no     = Column(String(30), nullable=False)
    customer_name = Column(String(200), nullable=True)
    # Snapshotted at build: the same number can sit in two phonebooks with two
    # different agents, and the campaign must record which one it sent under.
    agent_id      = Column(String(100), nullable=True)
    # Ordered template parameters, already resolved. The send worker never has to
    # know whether they came from shared_params or from a CSV row.
    params        = Column(JSONB, nullable=True)

    # This recipient's own header image, personalised per-row mode only. Stored as
    # the URL rather than a Meta media id: each row is one message, so Meta fetches
    # each address exactly once — the amplification that made a shared link wrong
    # (one URL fetched N times) does not arise here. Reachability is checked at
    # import instead, so a dead link is a reported skipped row, not a failed send.
    header_media_url = Column(String(2048), nullable=True)

    # draft → pending → sending → sent → delivered → read
    #                          ↘ unconfirmed (accepted, no receipt within the timeout)
    #                          ↘ failed
    #         skipped (never sent — see skip_reason)
    #
    # "sent" means Meta accepted the request. It is NOT evidence the message reached
    # a phone, and nothing downstream may treat it as such. A row that stays "sent"
    # past the company's receipt_timeout_minutes becomes "unconfirmed" — the system
    # saying it does not know, rather than implying delivery is still coming.
    status      = Column(String(20), nullable=False, default="draft")
    skip_reason = Column(String(40), nullable=True)   # opted_out | invalid_flagged | us_number | validation

    # Meta's message id. The only link back from a delivery receipt to this row,
    # since broadcast writes no Message rows.
    wamid         = Column(String(200), nullable=True)
    error_code    = Column(String(20), nullable=True)
    error_message = Column(String(500), nullable=True)

    # Set when the worker claims this row, cleared when it reaches a terminal state.
    # Crash recovery uses the age of this stamp to tell a row stranded by a dead
    # process from one another instance is actively sending. Without it, a booting
    # instance resets every "sending" row — including in-flight ones — and those
    # recipients are messaged twice.
    claimed_at   = Column(DateTime(timezone=True), nullable=True)

    # Retry bookkeeping. Only transient failures are retried — a rejection Meta will
    # repeat identically (invalid number, parameter mismatch) is terminal on the first
    # attempt, because retrying it burns quota and signals bad list hygiene to Meta.
    # next_attempt_at NULL means "eligible now", which is what every existing row
    # backfills to.
    attempts        = Column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)

    sent_at      = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    read_at      = Column(DateTime(timezone=True), nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # Dedup as a database guarantee, not merely a build-time check: a number in
        # two phonebooks is sent only once. Meta bills unique recipients, and being
        # messaged twice by the same campaign reads as a mistake.
        UniqueConstraint("campaign_id", "mobile_no", name="uq_broadcast_recipient_campaign_mobile"),
        # The worker's claim query filters on exactly this pair.
        Index("ix_broadcast_recipients_campaign_status", "campaign_id", "status"),
        # Status-receipt lookup — the §8 fallback path.
        Index("ix_broadcast_recipients_wamid", "wamid"),
        # "which campaigns has this customer been sent?" for the contact panel.
        Index("ix_broadcast_recipients_mobile_no", "mobile_no"),
        # The claim query filters on all three; without this a retry-heavy campaign
        # turns every 3-second tick into a scan of its recipients.
        Index("ix_broadcast_recipients_next_attempt",
              "campaign_id", "status", "next_attempt_at"),
    )
