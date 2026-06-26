from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.core.database import Base


class ErrorLog(Base):
    __tablename__ = "error_log"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    title        = Column(String(255), nullable=False)
    method       = Column(String(255), nullable=False)
    error_type   = Column(String(120), nullable=False)
    traceback    = Column(Text, nullable=False)
    request_data = Column(JSONB, nullable=True)
    user         = Column(String(255), nullable=True)
    ip_address   = Column(String(45), nullable=True)
    seen         = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at   = Column(DateTime(timezone=True), server_default=func.now())


Index("ix_error_log_seen_created", ErrorLog.seen, ErrorLog.created_at)
