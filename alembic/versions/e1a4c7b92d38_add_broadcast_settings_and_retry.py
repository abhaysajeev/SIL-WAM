"""add broadcast_settings and per-recipient retry state

Revision ID: e1a4c7b92d38
Revises: d5a91b3e7c04
Create Date: 2026-09-07 10:12:03.418772

Two related changes, together because the retry columns are useless without the settings
that bound them.

**broadcast_settings** — one row per company, no inheritance. Every default equals the
constant that was compiled into broadcast_scheduler.py before this table existed, so
applying this migration changes no send behaviour on its own. The exceptions are
deliberate: send_concurrency (the loop was serial) and max_retries (a failure was
permanent).

Existing companies are backfilled here. New companies are seeded by the company create
API, and the accessor falls back to the same defaults if a row is somehow missing — a
company must never be unable to send because nobody visited a settings page.

**broadcast_recipients.attempts / next_attempt_at** — retry bookkeeping. Backfilled to
0/NULL, which reads as "never attempted, eligible now" and so leaves in-flight campaigns
behaving exactly as they did.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = 'e1a4c7b92d38'
down_revision: Union[str, None] = 'd5a91b3e7c04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "broadcast_settings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", UUID(as_uuid=True),
                  sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_max_recipients_per_send", sa.Integer(), nullable=False,
                  server_default=sa.text("100")),
        sa.Column("agent_daily_message_limit", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("send_concurrency", sa.Integer(), nullable=False,
                  server_default=sa.text("10")),
        sa.Column("batch_size", sa.Integer(), nullable=False,
                  server_default=sa.text("20")),
        sa.Column("max_retries", sa.Integer(), nullable=False,
                  server_default=sa.text("2")),
        sa.Column("retry_backoff_seconds", sa.Integer(), nullable=False,
                  server_default=sa.text("30")),
        sa.Column("receipt_timeout_minutes", sa.Integer(), nullable=False,
                  server_default=sa.text("15")),
        sa.Column("stale_claim_minutes", sa.Integer(), nullable=False,
                  server_default=sa.text("5")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("company_id", name="broadcast_settings_company_id_key"),
    )
    op.create_index("ix_broadcast_settings_company_id", "broadcast_settings", ["company_id"])

    # Backfill. Every column has a server_default, so naming only company_id gives each
    # existing company exactly the behaviour it had before this migration ran.
    op.execute("""
        INSERT INTO broadcast_settings (id, company_id)
        SELECT gen_random_uuid(), c.id FROM companies c
        ON CONFLICT (company_id) DO NOTHING
    """)

    op.add_column("broadcast_recipients",
                  sa.Column("attempts", sa.Integer(), nullable=False,
                            server_default=sa.text("0")))
    op.add_column("broadcast_recipients",
                  sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    # The claim query filters on this alongside campaign_id and status; without it a
    # retry-heavy campaign turns every tick into a sequential scan of its recipients.
    op.create_index("ix_broadcast_recipients_next_attempt", "broadcast_recipients",
                    ["campaign_id", "status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_broadcast_recipients_next_attempt", table_name="broadcast_recipients")
    op.drop_column("broadcast_recipients", "next_attempt_at")
    op.drop_column("broadcast_recipients", "attempts")
    op.drop_index("ix_broadcast_settings_company_id", table_name="broadcast_settings")
    op.drop_table("broadcast_settings")
