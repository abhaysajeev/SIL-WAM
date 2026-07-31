import uuid

from sqlalchemy import Boolean, Column, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.database import Base


class Company(Base):
    __tablename__ = "companies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    company_code = Column(String(50), unique=True, nullable=False, index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Declared so `alembic revision --autogenerate` matches what the database
    # actually contains. Alembic compares by NAME: anything present in the DB but
    # not declared here is reported as an orphan and proposed for DROP.
    __table_args__ = (
        UniqueConstraint("company_code", name="companies_company_code_key"),
    )
