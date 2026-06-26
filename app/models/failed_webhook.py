from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.core.database import Base


class FailedWebhook(Base):
    __tablename__ = "failed_webhooks"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    source      = Column(String(50), nullable=False)   # "meta" | "erpnext"
    raw_payload = Column(JSONB)
    error_type  = Column(String(200))
    traceback   = Column(Text)
    replayed    = Column(Boolean, default=False, nullable=False)
