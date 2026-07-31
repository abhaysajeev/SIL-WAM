import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.database import Base


class Role(Base):
    __tablename__ = "roles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    is_system = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Declared so `alembic revision --autogenerate` matches what the database
    # actually contains. Alembic compares by NAME: anything present in the DB but
    # not declared here is reported as an orphan and proposed for DROP.
    __table_args__ = (
        UniqueConstraint("name", name="roles_name_key"),
    )

class RolePagePermission(Base):
    __tablename__ = "role_page_permission"

    role_id = Column(
        UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    page_name = Column(String(50), primary_key=True, nullable=False)
    can_read = Column(Boolean, nullable=False, default=False)
    can_create = Column(Boolean, nullable=False, default=False)
    can_write = Column(Boolean, nullable=False, default=False)
    can_delete = Column(Boolean, nullable=False, default=False)
