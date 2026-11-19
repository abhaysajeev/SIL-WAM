"""add webhook_inbox and whatsapp_accounts.last_webhook_at

Revision ID: b1d4e7a20c31
Revises: a7c3e1f5b982
Create Date: 2026-11-13 12:00:00

Store every Meta webhook before processing it (see app/models/webhook_inbox.py), and
record when each number last heard from Meta so a silently rejected webhook raises an
alarm instead of being discovered from a stuck campaign.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = 'b1d4e7a20c31'
down_revision: Union[str, None] = 'a7c3e1f5b982'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_inbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(30), nullable=False, server_default=sa.text("'meta'")),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("payload_sha256", name="uq_webhook_inbox_payload_sha256"),
    )
    op.create_index("ix_webhook_inbox_status_next", "webhook_inbox",
                    ["status", "next_attempt_at"])
    op.create_index("ix_webhook_inbox_received", "webhook_inbox", ["received_at"])

    op.add_column("whatsapp_accounts",
                  sa.Column("last_webhook_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("whatsapp_accounts", "last_webhook_at")
    op.drop_index("ix_webhook_inbox_received", table_name="webhook_inbox")
    op.drop_index("ix_webhook_inbox_status_next", table_name="webhook_inbox")
    op.drop_table("webhook_inbox")
