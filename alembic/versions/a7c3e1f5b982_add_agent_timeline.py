"""add agent_threads and agent_messages

Revision ID: a7c3e1f5b982
Revises: f2b5d8c03e49
Create Date: 2026-09-07 11:48:12.663401

The per-contact timeline the agent chat window reads.

A customer's history is currently split across two stores that do not know about each
other — broadcast sends in `broadcast_recipients`, replies in `messages` — so neither can
answer "show me everything involving this customer". These tables are the third store
that can.

**No backfill.** History starts the day this ships. Backfilling from
`broadcast_recipients` is possible and deliberately not done: it is a one-way migration,
and the value of retrofitting sends nobody replied to is low next to the risk of
inventing thread activity that never happened.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = 'a7c3e1f5b982'
down_revision: Union[str, None] = 'f2b5d8c03e49'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_threads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", UUID(as_uuid=True),
                  sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mobile_no", sa.String(30), nullable=False),
        sa.Column("contact_name", sa.String(200), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_seq", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("company_id", "mobile_no", name="uq_agent_thread_company_mobile"),
    )
    op.create_index("ix_agent_threads_company_id", "agent_threads", ["company_id"])
    op.create_index("ix_agent_threads_company_activity", "agent_threads",
                    ["company_id", "last_message_at"])

    op.create_table(
        "agent_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("thread_id", UUID(as_uuid=True),
                  sa.ForeignKey("agent_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("body_rendered", sa.Text(), nullable=True),
        sa.Column("template_meta", JSONB, nullable=True),
        sa.Column("media", JSONB, nullable=True),
        sa.Column("wamid", sa.String(200), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("error_code", sa.String(20), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("campaign_id", UUID(as_uuid=True),
                  sa.ForeignKey("broadcast_campaigns.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sent_by_agent_id", UUID(as_uuid=True),
                  sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("replies_to_wamid", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("thread_id", "seq", name="uq_agent_message_thread_seq"),
    )
    op.create_index("ix_agent_messages_thread_seq", "agent_messages", ["thread_id", "seq"])
    op.create_index("ix_agent_messages_wamid", "agent_messages", ["wamid"])


def downgrade() -> None:
    op.drop_index("ix_agent_messages_wamid", table_name="agent_messages")
    op.drop_index("ix_agent_messages_thread_seq", table_name="agent_messages")
    op.drop_table("agent_messages")
    op.drop_index("ix_agent_threads_company_activity", table_name="agent_threads")
    op.drop_index("ix_agent_threads_company_id", table_name="agent_threads")
    op.drop_table("agent_threads")
