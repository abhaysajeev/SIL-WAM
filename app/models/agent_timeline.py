"""
The per-contact message timeline the agent app's chat window reads.

**Why this exists at all.** A customer's history is currently split across two stores
that do not know about each other: broadcast sends live only in `broadcast_recipients`
(BROADCAST_DESIGN.md §8 keeps 2,000-row campaigns out of the transactional tables), while
their replies become `Message` rows under a `Conversation`. Verified on live data for one
number: 1 broadcast row, 190 message rows. Neither table alone can answer "show me
everything involving this customer", which is exactly what a chat window is.

So this is a third store, and it is deliberately a *timeline* rather than a second
transactional model: append-only, ordered, and read in one direction.

**The cost, stated plainly.** A `wamid` now lives in two places and a delivery receipt
updates both. `broadcast_recipients` remains the **campaign's** record — counters,
screening, retries, insights. `agent_messages` is the **customer's** record. Different
questions, different tables. The alternative considered and rejected was UNIONing the two
at read time: no duplication, but no gap-free `seq` either, so half the timeline would
need a second ordering scheme and the sync protocol would have to trust timestamps.
"""
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.core.database import Base


class AgentThread(Base):
    """
    One customer, per company. The container the chat window opens.

    Threads are created lazily by whichever side speaks first, and a thread existing is
    NOT the same as a thread belonging in an agent's inbox: a 2,000-recipient campaign
    creates 2,000 threads, and an inbox showing all of them would be useless. The inbox
    filters on `last_inbound_at IS NOT NULL` — people who actually wrote back — while
    opening a contact from a phonebook shows the thread regardless, so prior broadcasts
    are visible even when nobody replied.
    """
    __tablename__ = "agent_threads"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Canonical stored form, as resolved by conversation_engine._resolve_stored_mobile.
    # Threads keyed on the raw Meta number would split one customer in two.
    mobile_no  = Column(String(30), nullable=False)

    # Best known name, refreshed opportunistically from whatever wrote last. Display
    # only — never a key.
    contact_name = Column(String(200), nullable=True)

    # **The 24-hour clock.** WhatsApp allows free text only within 24 hours of the
    # customer's last inbound message. NULL means they have never written to us, and
    # the window has therefore never been open. Sending never sets this: a template
    # going out does not open a window, and treating it as if it did produces sends
    # Meta rejects with 131047.
    last_inbound_at = Column(DateTime(timezone=True), nullable=True)

    # Either direction. The inbox sort key.
    last_message_at = Column(DateTime(timezone=True), nullable=True)

    # Allocated with UPDATE ... RETURNING inside the writing transaction, so the row
    # lock serialises concurrent writers and the sequence is gap-free per thread. A
    # timestamp cannot do this job: under concurrency commit order and timestamp order
    # diverge, so a message can commit *after* one the client has already seen and be
    # missed by an "everything since X" sync forever.
    message_seq = Column(Integer, nullable=False, server_default=text("0"))

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("company_id", "mobile_no", name="uq_agent_thread_company_mobile"),
        # The inbox: this company's threads, most recent first.
        Index("ix_agent_threads_company_activity", "company_id", "last_message_at"),
    )


class AgentMessage(Base):
    """
    One message in one thread, inbound or outbound, whatever produced it.

    Append-only. Statuses are updated in place by delivery receipts; the row itself is
    never rewritten or removed, because the chat window is a record of what was said.
    """
    __tablename__ = "agent_messages"

    id        = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Position within the thread, from 1. See AgentThread.message_seq.
    seq = Column(Integer, nullable=False)

    direction = Column(String(10), nullable=False)   # inbound | outbound
    # broadcast | agent_reply | template | transactional — what produced this row,
    # so the chat window can label a bubble ("Campaign: Diwali offer") without
    # joining back to whichever table owns the original.
    kind      = Column(String(20), nullable=False)

    # **What the customer actually sees**, placeholders already substituted. Rendered
    # server-side from the same components sent to Meta, so a bubble can never disagree
    # with what was delivered. For inbound, the message text as received.
    body_rendered = Column(Text, nullable=True)

    # { name, language, params, header, buttons } for a template send — the structured
    # form beside the rendered one, so a template can be audited or resent without
    # parsing the prose back apart.
    template_meta = Column(JSONB, nullable=True)
    # { type, link|media_id, filename } — an image header, or an inbound attachment.
    media         = Column(JSONB, nullable=True)

    # Meta's id. How a delivery receipt finds this row, and how an inbound reply
    # carrying context.id finds the message it answers.
    wamid = Column(String(200), nullable=True)

    # queued | sent | delivered | read | failed | received (inbound)
    status        = Column(String(20), nullable=False, server_default=text("'queued'"))
    error_code    = Column(String(20), nullable=True)
    error_message = Column(String(500), nullable=True)

    # Set when kind = broadcast. SET NULL so deleting a campaign does not erase the
    # customer's history of having been messaged.
    campaign_id = Column(
        UUID(as_uuid=True),
        ForeignKey("broadcast_campaigns.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Which agent sent it, when one did. SET NULL for the same reason.
    sent_by_agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The message this one replies to, from Meta's context.id. Present only when the
    # customer used WhatsApp's reply affordance — most just type, so this is usually
    # NULL and must never be the only way a reply is attached to a thread.
    replies_to_wamid = Column(String(200), nullable=True)

    created_at   = Column(DateTime(timezone=True), server_default=func.now())
    sent_at      = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    read_at      = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Gap-free ordering as a database guarantee, not a convention. Two writers
        # racing on one thread cannot produce the same seq.
        UniqueConstraint("thread_id", "seq", name="uq_agent_message_thread_seq"),
        # The chat window's only read pattern: newest N in a thread, then page
        # backwards on seq. Keyset, never OFFSET, so page 40 costs what page 1 does.
        Index("ix_agent_messages_thread_seq", "thread_id", "seq"),
        # Delivery receipts arrive knowing only the wamid.
        Index("ix_agent_messages_wamid", "wamid"),
    )
