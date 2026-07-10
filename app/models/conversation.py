"""
Conversation engine models — core of the new architecture.

Hierarchy:
    companies
        └── conversations  (unique per mobile_no + company_id)
                ├── services       (one per client service_id / order — multiple can
                │       │           be in_progress concurrently for the same mobile_no)
                │       ├── service_responses  (one per answered question)
                │       └── mobile_queue       (per-service in_progress marker)
                └── messages       (every WA message, both directions)
"""
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.core.database import Base


class Conversation(Base):
    """One row per unique (mobile_no, company_id) pair."""
    __tablename__ = "conversations"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id       = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    mobile_no        = Column(String(30), nullable=False)
    first_contact_at = Column(DateTime(timezone=True), server_default=func.now())
    last_activity_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    total_messages   = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("mobile_no", "company_id", name="uq_conversation_mobile_company"),
    )


class Service(Base):
    """
    One row per service flow (order / interaction) initiated by a client.
    Replaces the old sales_orders table.

    status values:   waiting | in_progress | completed | expired | failed
    expired_reason:  timeout | superseded
    failed_reason:   whatsapp_number_invalid | send_error

    Concurrency is unlimited: multiple services can be status="in_progress" for
    the same (company_id, mobile_no) at once — see queue_manager.enqueue_service.
    """
    __tablename__ = "services"

    id                   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id      = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    company_id           = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    # Client's own reference ID (e.g. "ORD-2026-1042") — unique per company, not globally
    service_id           = Column(String(200), nullable=False, index=True)
    template_id          = Column(UUID(as_uuid=True),
                                  ForeignKey("whatsapp_templates.id", ondelete="SET NULL"),
                                  nullable=True)
    # Which CompanyApiKey ingested this service — resolves notify_url for outbound
    # status notifications. Null for services created outside client-api (demo, ERPNext).
    api_key_id           = Column(UUID(as_uuid=True),
                                  ForeignKey("company_api_keys.id", ondelete="SET NULL"),
                                  nullable=True, index=True)
    status               = Column(String(30), nullable=False, default="waiting", index=True)
    # False until the background send_scheduler has attempted the Meta template send.
    # A service can be status="in_progress" with template_sent=False — it's the active
    # flow for its mobile number but the actual Graph API call is still pending pickup.
    template_sent        = Column(Boolean, nullable=False, default=False, index=True)
    expired_reason       = Column(String(50), nullable=True)
    failed_reason        = Column(String(100), nullable=True)
    # Client-owned payload stored as-is: order details, customer info, etc.
    data                 = Column(JSONB, nullable=True)
    # Questions array with per-question sent flags (0=not sent, 1=sent)
    questions            = Column(JSONB, nullable=True)
    # Ordered body params for template {{1}}, {{2}}, ... substitutions
    template_params      = Column(JSONB, nullable=True)
    # URL button overrides {button_index_str: url_value}
    cta_urls             = Column(JSONB, nullable=True)
    template_expiry_hours = Column(Integer, nullable=False, default=24)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    completed_at         = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("service_id", "company_id", name="uq_service_id_company"),
    )


class MobileQueue(Base):
    """
    Per-service "in_progress" marker for (company_id, mobile_no). Historically a FIFO
    queue enforcing one in_progress service per mobile; concurrency is now unlimited,
    so multiple rows with status="in_progress" can coexist for the same mobile_no.
    `position` is vestigial (always 1) — retained for schema stability, not used for
    ordering anymore.
    """
    __tablename__ = "mobile_queue"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    mobile_no  = Column(String(30), nullable=False)
    service_id = Column(UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"),
                        nullable=False)
    position   = Column(Integer, nullable=False)
    status     = Column(String(20), nullable=False, default="waiting", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ServiceResponse(Base):
    """One row per question answered within a service flow."""
    __tablename__ = "service_responses"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id     = Column(UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    sequence       = Column(Integer, nullable=False)
    field_key      = Column(String(100), nullable=False)
    question       = Column(Text, nullable=False)
    answer_type    = Column(Integer, nullable=False)  # 0=yes/no buttons  1=rating  2=free text
    response_value = Column(Text, nullable=True)
    responded_at   = Column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    """
    Every single WhatsApp message in both directions.

    direction:     inbound | outbound
    message_type:  text | template | interactive | button | document | image
    status:        sent | delivered | read | failed  (outbound only)
    is_flow_message: False for random customer messages not part of any service flow
    """
    __tablename__ = "messages"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    service_id      = Column(UUID(as_uuid=True), ForeignKey("services.id", ondelete="SET NULL"),
                             nullable=True, index=True)
    wamid           = Column(String(200), nullable=True, unique=True, index=True)
    direction       = Column(String(10), nullable=False)
    message_type    = Column(String(30), nullable=False)
    content         = Column(JSONB, nullable=True)
    is_flow_message = Column(Boolean, nullable=False, default=True)
    status          = Column(String(20), nullable=True)
    sent_at         = Column(DateTime(timezone=True), nullable=True)
    delivered_at    = Column(DateTime(timezone=True), nullable=True)
    read_at         = Column(DateTime(timezone=True), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
