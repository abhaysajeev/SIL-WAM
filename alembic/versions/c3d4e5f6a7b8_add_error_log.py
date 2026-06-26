"""add error_log table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "error_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("method", sa.String(255), nullable=False),
        sa.Column("error_type", sa.String(120), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=False),
        sa.Column("request_data", postgresql.JSONB(), nullable=True),
        sa.Column("user", sa.String(255), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("seen", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_error_log_seen_created",
        "error_log",
        ["seen", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_error_log_seen_created", table_name="error_log")
    op.drop_table("error_log")
