"""
Every Meta webhook, stored before it is processed.

Meta delivers at least once and expects a 200 within seconds. The old handler acknowledged
and then processed in a FastAPI BackgroundTask, so an event lived only in memory between
the 200 and the commit: a restart in that gap lost it, and Meta — having had its 200 —
never sent it again. Writing the raw payload here first, and acknowledging only after that
commit, means every acknowledged event is on disk and can be processed, retried or replayed.

Status lifecycle:
    pending     → waiting to be claimed (new, or a failed attempt with next_attempt_at)
    processing  → claimed by a worker until `lease_until`; an expired lease is reclaimed
    done        → processed (handlers are idempotent, so a reclaim after a crash is safe)
    dead        → failed `MAX_ATTEMPTS` times; also copied to failed_webhooks for replay
    shadow      → stored while WEBHOOK_INBOX_MODE=shadow; the old path processed it

`payload_sha256` is the dedup key. Meta's retries resend the identical body, so the same
delivery collapses to one row; two different events never share a hash.
"""
from sqlalchemy import (
    BigInteger, Column, DateTime, Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.core.database import Base


class WebhookInbox(Base):
    __tablename__ = "webhook_inbox"

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    source          = Column(String(30), nullable=False, server_default=text("'meta'"))
    payload         = Column(JSONB, nullable=False)
    payload_sha256  = Column(String(64), nullable=False)
    status          = Column(String(20), nullable=False, server_default=text("'pending'"))
    attempts        = Column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    lease_until     = Column(DateTime(timezone=True), nullable=True)
    last_error      = Column(Text, nullable=True)
    received_at     = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at    = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("payload_sha256", name="uq_webhook_inbox_payload_sha256"),
        Index("ix_webhook_inbox_status_next", "status", "next_attempt_at"),
        Index("ix_webhook_inbox_received", "received_at"),
    )
