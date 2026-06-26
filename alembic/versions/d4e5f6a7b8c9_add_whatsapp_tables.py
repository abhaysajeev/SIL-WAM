"""add whatsapp_accounts and whatsapp_onboarding_sessions tables

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("waba_id", sa.String(100), nullable=True),
        sa.Column("phone_number_id", sa.String(100), nullable=True),
        sa.Column("display_phone_number", sa.String(50), nullable=True),
        sa.Column("business_name", sa.String(200), nullable=True),
        sa.Column("business_id", sa.String(100), nullable=True),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("token_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connection_status", sa.String(20), server_default="disconnected", nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", name="uq_whatsapp_accounts_company_id"),
    )
    op.create_index("ix_whatsapp_accounts_company_id", "whatsapp_accounts", ["company_id"])

    op.create_table(
        "whatsapp_onboarding_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_step", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(20), server_default="in_progress", nullable=False),
        sa.Column("last_completed_step", sa.Integer(), server_default="0", nullable=False),
        sa.Column("meta_state", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_whatsapp_onboarding_company_status",
        "whatsapp_onboarding_sessions",
        ["company_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_whatsapp_onboarding_company_status", table_name="whatsapp_onboarding_sessions")
    op.drop_table("whatsapp_onboarding_sessions")
    op.drop_index("ix_whatsapp_accounts_company_id", table_name="whatsapp_accounts")
    op.drop_table("whatsapp_accounts")
