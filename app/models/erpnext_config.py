import uuid

from sqlalchemy import Boolean, Column, DateTime, String, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.database import Base


class ERPNextConfig(Base):
    """Per-company ERPNext integration credentials.

    One row per company. Holds the base URL and API key/secret SIL-WAM uses
    when calling back to the company's ERPNext instance (e.g. to trigger invoice
    PDF generation and WhatsApp delivery after a QUICK_REPLY button tap).
    """
    __tablename__ = "erpnext_configs"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    base_url   = Column(String(500), nullable=False)
    api_key    = Column(String(200), nullable=False)
    api_secret = Column(String(200), nullable=False)
    # Per-company override for the ERPNext whitelisted method to call.
    # Falls back to settings.ERPNEXT_PDF_METHOD when NULL.
    pdf_method = Column(String(300), nullable=True)
    is_active  = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
