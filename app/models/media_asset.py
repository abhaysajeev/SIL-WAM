"""
One stored media file — a broadcast header image an agent uploaded, or (Phase 4) an
attachment a customer sent.

**One file, many uses.** A standard broadcast to 500 people is ONE row here: one copy in
our store and one upload to Meta (`meta_media_id`), reused for every recipient and served
to every agent who opens any of those 500 chats. Nothing is copied per recipient.

Retention: every file is deleted `MEDIA_RETENTION_DAYS` (30) after it was stored —
`expires_at` — by the daily job in media_assets.py, which sets `deleted_at`. The row is
kept, so a chat bubble can still say "no longer available" instead of breaking.
"""
import uuid

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.database import Base


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
                        nullable=False)
    uploaded_by_agent_id = Column(UUID(as_uuid=True),
                                  ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    uploaded_by_user_id  = Column(UUID(as_uuid=True),
                                  ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    direction  = Column(String(10), nullable=False, server_default=text("'outbound'"))
    # Meta's header/media type: IMAGE | VIDEO | DOCUMENT (AUDIO/STICKER for inbound).
    format     = Column(String(20), nullable=False)
    mime       = Column(String(100), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    sha256     = Column(String(64), nullable=False)
    filename   = Column(String(255), nullable=True)
    storage_key = Column(String(500), nullable=False)

    # Meta's id for the same bytes, valid for sending for 30 days from upload.
    meta_media_id         = Column(String(100), nullable=True)
    meta_media_expires_at = Column(DateTime(timezone=True), nullable=True)

    expires_at = Column(DateTime(timezone=True), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_media_assets_company", "company_id"),
        # The retention job's scan: live files past their expiry.
        Index("ix_media_assets_expiry", "expires_at",
              postgresql_where=text("deleted_at IS NULL")),
    )
