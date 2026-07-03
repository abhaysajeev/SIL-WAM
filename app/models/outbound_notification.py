"""
Durable outbound delivery queue for client status callbacks.

One row per notification event (message sent/delivered/read, or service-level
completed/expired/failed). payload is fully materialized at enqueue time —
notify_scheduler.py just POSTs the stored JSON, no re-computation at send time.
"""
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.core.database import Base


class OutboundNotification(Base):
    __tablename__ = "outbound_notifications"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id      = Column(UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    # Null for service-level events (completed/expired/failed) — set only for
    # message-level events (sent/delivered/read) tied to one specific Message.
    message_id      = Column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"),
                             nullable=True, index=True)
    notify_url      = Column(String(500), nullable=False)  # snapshotted from CompanyApiKey at enqueue time
    payload         = Column(JSONB, nullable=False)
    status          = Column(String(20), nullable=False, default="pending", index=True)  # pending|delivered|failed
    attempts        = Column(Integer, nullable=False, default=0)
    last_error      = Column(Text, nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    delivered_at    = Column(DateTime(timezone=True), nullable=True)
