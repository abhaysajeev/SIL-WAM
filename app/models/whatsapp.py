import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.core.database import Base


class WhatsAppAccount(Base):
    __tablename__ = "whatsapp_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Uniqueness is enforced by uq_whatsapp_accounts_company_id below, not by the
    # column — the database's ix_whatsapp_accounts_company_id is a plain index, and
    # unique=True here would make autogenerate try to convert it every run.
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    waba_id = Column(String(100), nullable=True)
    phone_number_id = Column(String(100), nullable=True)
    display_phone_number = Column(String(50), nullable=True)
    business_name = Column(String(200), nullable=True)
    business_id = Column(String(100), nullable=True)
    access_token_encrypted = Column(Text, nullable=True)
    token_expiry = Column(DateTime(timezone=True), nullable=True)
    connection_status = Column(String(20), nullable=False, default="disconnected")
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Declared so autogenerate matches the database — see app/models/company.py.
    __table_args__ = (
        UniqueConstraint("company_id", name="uq_whatsapp_accounts_company_id"),
    )

class WhatsAppOnboardingSession(Base):
    __tablename__ = "whatsapp_onboarding_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # No index=True: the database indexes (company_id, status) compositely, which
    # already covers company_id lookups as a prefix. Declaring a single-column
    # index here would make autogenerate try to create one on every run.
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                        nullable=False)
    current_step = Column(Integer, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="in_progress")
    last_completed_step = Column(Integer, nullable=False, default=0)
    meta_state = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Declared so autogenerate matches the database — see app/models/company.py.
    __table_args__ = (
        Index("ix_whatsapp_onboarding_company_status", "company_id", "status"),
    )

class WhatsAppTemplate(Base):
    __tablename__ = "whatsapp_templates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    waba_id = Column(String(100), nullable=False)
    meta_template_id = Column(String(100), nullable=True)
    name = Column(String(512), nullable=False)
    category = Column(String(20), nullable=False)
    language = Column(String(10), nullable=False, default="en_US")
    status = Column(String(30), nullable=False, default="PENDING")
    components = Column(JSONB, nullable=False, default=list)
    rejection_reason = Column(String(500), nullable=True)
    param_mapping  = Column(JSONB, nullable=True)    # {"1": "customer_name", "2": "order.amount"}
    cta_mapping    = Column(JSONB, nullable=True)    # {"0": "invoice_url"} — 0-indexed button pos
    mobile_mapping = Column(String(200), nullable=True)  # dot-path to phone number in data, e.g. "customer.phone"
    # Dot-path to the media URL for an IMAGE/DOCUMENT/VIDEO header, e.g. "data.receipt_image_url".
    # Only meaningful when components carries a HEADER with a non-TEXT format; the format itself
    # is read back out of components by template_body.header_format().
    header_mapping = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    synced_at = Column(DateTime(timezone=True), nullable=True)

    # Declared so autogenerate matches the database — see app/models/company.py.
    # uq_whatsapp_templates_company_name_lang is load-bearing: without it a company
    # could hold two APPROVED templates with the same name and language, and
    # client_services_api.ingest_service resolves templates with .first() — it would
    # silently pick one at random.
    __table_args__ = (
        Index("ix_whatsapp_templates_company_status", "company_id", "status"),
        UniqueConstraint("company_id", "name", "language",
                         name="uq_whatsapp_templates_company_name_lang"),
    )
