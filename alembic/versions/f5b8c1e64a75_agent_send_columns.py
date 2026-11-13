"""agent_messages: retry key, request hash, attempted_at, failure category, reactions

Revision ID: f5b8c1e64a75
Revises: e4a7b0d53f64
Create Date: 2026-11-13 18:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = 'f5b8c1e64a75'
down_revision: Union[str, None] = 'e4a7b0d53f64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_messages", sa.Column("client_msg_id", sa.String(64), nullable=True))
    op.add_column("agent_messages", sa.Column("request_hash", sa.String(64), nullable=True))
    op.add_column("agent_messages",
                  sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_messages", sa.Column("failure_category", sa.String(20), nullable=True))
    op.add_column("agent_messages", sa.Column("reactions", JSONB(), nullable=True))
    op.create_index("uq_agent_messages_client_msg", "agent_messages",
                    ["sent_by_agent_id", "client_msg_id"], unique=True,
                    postgresql_where=sa.text("client_msg_id IS NOT NULL"))
    op.create_index("ix_agent_messages_pending_send", "agent_messages",
                    ["status", "created_at"],
                    postgresql_where=sa.text("status IN ('queued', 'sending')"))


def downgrade() -> None:
    op.drop_index("ix_agent_messages_pending_send", table_name="agent_messages")
    op.drop_index("uq_agent_messages_client_msg", table_name="agent_messages")
    for col in ("reactions", "failure_category", "attempted_at", "request_hash",
                "client_msg_id"):
        op.drop_column("agent_messages", col)
