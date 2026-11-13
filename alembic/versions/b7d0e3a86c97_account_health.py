"""whatsapp_accounts: messaging tier, quality event, blocked reason

Revision ID: b7d0e3a86c97
Revises: a6c9d2f75b86
Create Date: 2026-11-13 20:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'b7d0e3a86c97'
down_revision: Union[str, None] = 'a6c9d2f75b86'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("whatsapp_accounts", sa.Column("messaging_limit_tier", sa.String(40), nullable=True))
    op.add_column("whatsapp_accounts", sa.Column("quality_event", sa.String(40), nullable=True))
    op.add_column("whatsapp_accounts", sa.Column("blocked_reason", sa.String(300), nullable=True))
    op.add_column("whatsapp_accounts",
                  sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ("blocked_at", "blocked_reason", "quality_event", "messaging_limit_tier"):
        op.drop_column("whatsapp_accounts", col)
