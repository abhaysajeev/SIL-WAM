import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.core.database import Base


class WhatsAppAccount(Base):
    __tablename__ = "whatsapp_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                        unique=True, nullable=False, index=True)
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


class WhatsAppOnboardingSession(Base):
    __tablename__ = "whatsapp_onboarding_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    current_step = Column(Integer, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="in_progress")
    last_completed_step = Column(Integer, nullable=False, default=0)
    meta_state = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


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
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    synced_at = Column(DateTime(timezone=True), nullable=True)
