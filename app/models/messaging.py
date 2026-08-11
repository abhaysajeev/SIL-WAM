"""
Opt-outs and invalid numbers — two tables, deliberately not one.

They look similar (a company-scoped list of numbers not to message) but they behave
differently, and merging them behind a `type` column would tempt someone to treat them
alike:

                  | Opt-out                    | Invalid number
    Meaning       | the person chose to stop   | technical delivery failure
    Lifecycle     | permanent until opt-in     | may become valid later
    At screening  | hard skip, never send      | warn, allow override
    Source        | STOP / button / manual     | Meta error 131026

Opt-out is compliance; invalid is deliverability. Sending to an opted-out number is a
policy breach, sending to a previously-invalid one is merely likely to fail.

Both are **company-scoped**. Consent belongs to a business — unsubscribing from one
company must not unsubscribe you from another that happens to share this platform.
"""
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.database import Base


class MessagingOptOut(Base):
    """A number that has asked this company to stop messaging it."""
    __tablename__ = "messaging_optouts"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    mobile_no  = Column(String(30), nullable=False)
    # stop_keyword | optout_button | manual | api
    # Only manual/api are written today; inbound STOP detection is not built yet.
    source       = Column(String(30), nullable=False, default="manual")
    opted_out_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("company_id", "mobile_no", name="uq_optout_company_mobile"),
    )


class InvalidNumber(Base):
    """
    A number Meta has rejected as not on WhatsApp.

    Written from the async status webhook, not at send time: Meta routinely accepts a
    send and reports 131026 seconds later. Kept as a warning rather than a block —
    a number can become valid, and a stale flag should not silently exclude someone.
    """
    __tablename__ = "invalid_numbers"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    mobile_no  = Column(String(30), nullable=False)
    error_code = Column(String(20), nullable=True)

    # Occurrences and last_seen_at let the UI show age and repetition instead of a bare
    # flag — "failed 4 times, last 3 days ago" is actionable in a way "invalid" is not.
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now())
    last_seen_at  = Column(DateTime(timezone=True), server_default=func.now())
    occurrences   = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "mobile_no", name="uq_invalid_company_mobile"),
    )
