import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.database import Base


class CompanyApiKey(Base):
    __tablename__ = "company_api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Generated once via secrets.token_urlsafe(48) — never exposed after creation
    api_key = Column(String(128), unique=True, nullable=False, index=True)
    label = Column(String(100), nullable=True)        # e.g. "dotnet-prod"
    notify_url = Column(String(500), nullable=True)   # Phase 4: template change webhook
    is_active = Column(Boolean, nullable=False, default=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
