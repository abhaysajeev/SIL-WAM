"""agent_thread_participants — which agents see a customer's chat

Revision ID: e4a7b0d53f64
Revises: d3f6a9c42e53
Create Date: 2026-11-13 17:00:00

Backfill: an agent who already sent a message recorded in a thread (sent_by_agent_id)
becomes a participant of it, with last_contact_at = their latest such message. Broadcast
rows written before this release carry no sender, so pre-existing broadcast threads are
visible to nobody until the customer is contacted again — the web panel still shows them.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = 'e4a7b0d53f64'
down_revision: Union[str, None] = 'd3f6a9c42e53'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_thread_participants",
        sa.Column("thread_id", UUID(as_uuid=True),
                  sa.ForeignKey("agent_threads.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("agent_id", UUID(as_uuid=True),
                  sa.ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("first_contact_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("last_contact_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_thread_participants_agent", "agent_thread_participants",
                    ["agent_id", "last_contact_at"])
    op.execute("""
        INSERT INTO agent_thread_participants
               (thread_id, agent_id, reason, first_contact_at, last_contact_at)
        SELECT m.thread_id, m.sent_by_agent_id,
               CASE WHEN bool_or(m.kind = 'broadcast') THEN 'broadcast' ELSE 'reply' END,
               MIN(m.created_at), MAX(m.created_at)
          FROM agent_messages m
         WHERE m.sent_by_agent_id IS NOT NULL
         GROUP BY m.thread_id, m.sent_by_agent_id
    """)


def downgrade() -> None:
    op.drop_index("ix_agent_thread_participants_agent", table_name="agent_thread_participants")
    op.drop_table("agent_thread_participants")
