"""add whatsapp_templates table

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("waba_id", sa.String(100), nullable=False),
        sa.Column("meta_template_id", sa.String(100), nullable=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("language", sa.String(10), server_default="en_US", nullable=False),
        sa.Column("status", sa.String(30), server_default="PENDING", nullable=False),
        sa.Column("components", postgresql.JSONB(), nullable=False),
        sa.Column("rejection_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "name", "language", name="uq_whatsapp_templates_company_name_lang"),
    )
    op.create_index("ix_whatsapp_templates_company_id", "whatsapp_templates", ["company_id"])
    op.create_index("ix_whatsapp_templates_company_status", "whatsapp_templates", ["company_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_whatsapp_templates_company_status", table_name="whatsapp_templates")
    op.drop_index("ix_whatsapp_templates_company_id", table_name="whatsapp_templates")
    op.drop_table("whatsapp_templates")
