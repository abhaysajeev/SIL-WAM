"""timeline rev counter, BSUID identity, default country for phone numbers

Revision ID: c2e5f8b31d42
Revises: b1d4e7a20c31
Create Date: 2026-11-13 13:00:00

* agent_threads.rev / agent_messages.rev — the change counter the app syncs on. Backfilled
  so that existing rows are all "already seen" by a client holding rev = message_seq:
  messages get rev = seq, threads get rev = message_seq.
* agent_threads.bsuid, mobile_no nullable — a customer can be known by WhatsApp username
  id alone. The (company, mobile_no) unique constraint becomes a partial unique index so
  number-less threads do not collide; a second partial index covers (company, bsuid).
* broadcast_settings.default_country — the country a number without a country code is
  read in (app/core/phone.py).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'c2e5f8b31d42'
down_revision: Union[str, None] = 'b1d4e7a20c31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_threads",
                  sa.Column("rev", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("agent_messages",
                  sa.Column("rev", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.execute("UPDATE agent_messages SET rev = seq")
    op.execute("UPDATE agent_threads SET rev = message_seq")
    op.create_index("ix_agent_messages_thread_rev", "agent_messages", ["thread_id", "rev"])

    op.add_column("agent_threads", sa.Column("bsuid", sa.String(160), nullable=True))
    op.alter_column("agent_threads", "mobile_no", existing_type=sa.String(30), nullable=True)
    op.drop_constraint("uq_agent_thread_company_mobile", "agent_threads", type_="unique")
    op.create_index("uq_agent_thread_company_mobile", "agent_threads",
                    ["company_id", "mobile_no"], unique=True,
                    postgresql_where=sa.text("mobile_no IS NOT NULL"))
    op.create_index("uq_agent_thread_company_bsuid", "agent_threads",
                    ["company_id", "bsuid"], unique=True,
                    postgresql_where=sa.text("bsuid IS NOT NULL"))

    op.add_column("broadcast_settings",
                  sa.Column("default_country", sa.String(2), nullable=False,
                            server_default=sa.text("'IN'")))


def downgrade() -> None:
    op.drop_column("broadcast_settings", "default_country")

    op.drop_index("uq_agent_thread_company_bsuid", table_name="agent_threads")
    op.drop_index("uq_agent_thread_company_mobile", table_name="agent_threads")
    # Username-only threads cannot survive a NOT NULL mobile_no.
    op.execute("DELETE FROM agent_threads WHERE mobile_no IS NULL")
    op.create_unique_constraint("uq_agent_thread_company_mobile", "agent_threads",
                                ["company_id", "mobile_no"])
    op.alter_column("agent_threads", "mobile_no", existing_type=sa.String(30), nullable=False)
    op.drop_column("agent_threads", "bsuid")

    op.drop_index("ix_agent_messages_thread_rev", table_name="agent_messages")
    op.drop_column("agent_messages", "rev")
    op.drop_column("agent_threads", "rev")
