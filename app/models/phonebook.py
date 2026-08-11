"""
PhoneBooks — reusable contact lists for broadcast campaigns.

    companies
     └─< phonebooks            (unique name per company)
          └─< phonebook_contacts   (unique mobile_no per phonebook)

A list belongs to one company; a company may have many. The same mobile
number may legitimately appear in several lists (a customer can be on both
a "VIP" and a "Diwali offer" list), so uniqueness is scoped to the phonebook,
not the company.

Contacts are NOT linked to conversations or services. A broadcast snapshots the
values it needs onto its own recipient rows at send time, so editing or deleting
a phonebook afterwards never rewrites the history of a campaign that already ran.
"""
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.database import Base


class Phonebook(Base):
    """A named contact list owned by one company."""
    __tablename__ = "phonebooks"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    name       = Column(String(150), nullable=False)
    # Who created it. SET NULL so deleting a user never removes their lists.
    created_by_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
                           nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_phonebook_company_name"),
    )


class PhonebookContact(Base):
    """One contact inside a phonebook."""
    __tablename__ = "phonebook_contacts"

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phonebook_id = Column(UUID(as_uuid=True),
                               ForeignKey("phonebooks.id", ondelete="CASCADE"),
                               nullable=False, index=True)
    # Normalised on the way in by _validate_mobile — digits only, no leading '+'.
    # Storing it any other way breaks matching against opt-out lists and inbound
    # webhooks, which is how service FACO050010 was lost.
    mobile_no     = Column(String(30), nullable=False, index=True)
    customer_name = Column(String(200), nullable=False)
    email         = Column(String(255), nullable=True)
    # Free-text identifier from the external agent app — deliberately not a FK.
    # There is no agents table yet, and the id originates in another system.
    agent_id      = Column(String(100), nullable=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
    updated_at    = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("phonebook_id", "mobile_no", name="uq_phonebook_contact_mobile"),
    )
