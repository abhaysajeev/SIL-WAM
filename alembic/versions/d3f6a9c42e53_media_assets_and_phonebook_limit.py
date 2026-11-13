"""media_assets table, broadcast_settings.agent_max_contacts_per_phonebook

Revision ID: d3f6a9c42e53
Revises: c2e5f8b31d42
Create Date: 2026-11-13 15:00:00

* media_assets — our stored copy of each broadcast header file (and, later, inbound
  attachments), with the Meta media id it was uploaded as and its 30-day expiry.
* agent_max_contacts_per_phonebook — the per-company ceiling on an agent-built phonebook,
  replacing the hard-coded 2,000. Default 2,000, so behaviour is unchanged until edited.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = 'd3f6a9c42e53'
down_revision: Union[str, None] = 'c2e5f8b31d42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("broadcast_settings",
                  sa.Column("agent_max_contacts_per_phonebook", sa.Integer(), nullable=False,
                            server_default=sa.text("2000")))

    op.create_table(
        "media_assets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", UUID(as_uuid=True),
                  sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("uploaded_by_agent_id", UUID(as_uuid=True),
                  sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("uploaded_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("direction", sa.String(10), nullable=False,
                  server_default=sa.text("'outbound'")),
        sa.Column("format", sa.String(20), nullable=False),
        sa.Column("mime", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("meta_media_id", sa.String(100), nullable=True),
        sa.Column("meta_media_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_media_assets_company", "media_assets", ["company_id"])
    op.create_index("ix_media_assets_expiry", "media_assets", ["expires_at"],
                    postgresql_where=sa.text("deleted_at IS NULL"))


def downgrade() -> None:
    op.drop_index("ix_media_assets_expiry", table_name="media_assets")
    op.drop_index("ix_media_assets_company", table_name="media_assets")
    op.drop_table("media_assets")
    op.drop_column("broadcast_settings", "agent_max_contacts_per_phonebook")
