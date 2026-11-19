"""push tokens on agent sessions; agent_threads.notified_rev

Revision ID: a6c9d2f75b86
Revises: f5b8c1e64a75
Create Date: 2026-11-13 19:00:00

notified_rev starts equal to rev, so deploying this does not push every existing change.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'a6c9d2f75b86'
down_revision: Union[str, None] = 'f5b8c1e64a75'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("push_token", sa.String(512), nullable=True))
    op.add_column("agent_sessions", sa.Column("push_platform", sa.String(20), nullable=True))
    op.add_column("agent_sessions",
                  sa.Column("push_token_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_threads", sa.Column("notified_rev", sa.Integer(), nullable=False,
                                             server_default=sa.text("0")))
    op.execute("UPDATE agent_threads SET notified_rev = rev")
    op.create_index("ix_agent_threads_unnotified", "agent_threads", ["id"],
                    postgresql_where=sa.text("rev > notified_rev"))


def downgrade() -> None:
    op.drop_index("ix_agent_threads_unnotified", table_name="agent_threads")
    op.drop_column("agent_threads", "notified_rev")
    for col in ("push_token_updated_at", "push_platform", "push_token"):
        op.drop_column("agent_sessions", col)
