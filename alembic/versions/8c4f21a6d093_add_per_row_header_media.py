"""add per-row header media for personalised campaigns

Revision ID: 8c4f21a6d093
Revises: 6b1d4a90f7c2
Create Date: 2026-09-01 14:41:18.552104

Backfill note: existing campaigns with a header_media get mode "shared", which is
what they are. Everything else stays NULL — meaning "this template has no media
header", which is distinct from per-row mode and must not be conflated with it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8c4f21a6d093'
down_revision: Union[str, None] = '6b1d4a90f7c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('broadcast_campaigns',
                  sa.Column('header_media_mode', sa.String(length=20), nullable=True))
    op.add_column('broadcast_recipients',
                  sa.Column('header_media_url', sa.String(length=2048), nullable=True))
    op.execute("""
        UPDATE broadcast_campaigns
        SET header_media_mode = 'shared'
        WHERE header_media IS NOT NULL
    """)


def downgrade() -> None:
    op.drop_column('broadcast_recipients', 'header_media_url')
    op.drop_column('broadcast_campaigns', 'header_media_mode')
