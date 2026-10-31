"""add header_media to broadcast_campaigns

Revision ID: 6b1d4a90f7c2
Revises: 3883433754d1
Create Date: 2026-09-01 10:12:04.118920

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '6b1d4a90f7c2'
down_revision: Union[str, None] = '3883433754d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'broadcast_campaigns',
        sa.Column('header_media', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('broadcast_campaigns', 'header_media')
