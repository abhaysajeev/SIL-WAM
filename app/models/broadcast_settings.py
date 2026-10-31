"""
Per-company broadcast tuning — one row per company, no inheritance.

Every value here was a hardcoded constant in broadcast_scheduler.py before this table
existed, and each default below is **exactly** the value that used to be compiled in.
Installing this table therefore changes no behaviour by itself; it only makes the knobs
reachable without a deploy. The two exceptions are deliberate improvements agreed with
the user: `send_concurrency` (the send loop was serial) and `max_retries` (a failure was
permanent, even a network blip).

**The poll interval is deliberately absent.** There is a single APScheduler job for the
whole install, so a per-company interval could not be honoured — it would be a setting
that silently does nothing, which is worse than not offering it.

Shaped after ERPNextConfig: company_id FK + unique, one row per company. That is the
established precedent here for per-company singleton config, and matching it means the
doctype list/form pattern works without a bespoke template.
"""
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.database import Base


class BroadcastSettings(Base):
    __tablename__ = "broadcast_settings"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )

    # ── Agent limits ────────────────────────────────────────────────────────
    # Ceiling on an agent-initiated broadcast. Admin-created campaigns are not
    # affected: an admin choosing a 2,000-contact phonebook meant to.
    agent_max_recipients_per_send = Column(
        Integer, nullable=False, server_default=text("100"))
    # Rolling 24h ceiling across everything one agent sends. 0 = unlimited.
    agent_daily_message_limit = Column(
        Integer, nullable=False, server_default=text("0"))

    # ── Throughput ──────────────────────────────────────────────────────────
    # Parallel sends per batch. Workers hold no database session, so this costs
    # threads, not connections from the pool the other schedulers share.
    send_concurrency = Column(Integer, nullable=False, server_default=text("10"))
    # Recipients claimed per scheduler tick. Was _BATCH_LIMIT.
    batch_size       = Column(Integer, nullable=False, server_default=text("20"))

    # ── Retry ───────────────────────────────────────────────────────────────
    # Transient failures only — a rejection Meta will repeat is never retried.
    # 0 restores the original behaviour of one attempt.
    max_retries           = Column(Integer, nullable=False, server_default=text("2"))
    retry_backoff_seconds = Column(Integer, nullable=False, server_default=text("30"))

    # ── Honest status ───────────────────────────────────────────────────────
    # How long a recipient may sit accepted-but-unconfirmed before the system
    # stops implying it might still arrive and marks it "unconfirmed".
    receipt_timeout_minutes = Column(
        Integer, nullable=False, server_default=text("15"))
    # How long a row may sit in "sending" before recovery treats it as stranded.
    # Must comfortably exceed wa_sender's 20s HTTP timeout. Was _CLAIM_STALE_AFTER.
    stale_claim_minutes = Column(Integer, nullable=False, server_default=text("5"))

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Declared so `alembic revision --autogenerate` matches the database. Alembic
    # compares by NAME: a constraint in the DB but not here is proposed for DROP.
    __table_args__ = (
        UniqueConstraint("company_id", name="broadcast_settings_company_id_key"),
    )
