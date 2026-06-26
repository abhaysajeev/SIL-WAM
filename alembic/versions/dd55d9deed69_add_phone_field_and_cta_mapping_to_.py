"""add phone_field and cta_mapping to webhook_configs

Revision ID: dd55d9deed69
Revises: d5e7a8c89541
Create Date: 2026-06-09 13:47:07.019387

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'dd55d9deed69'
down_revision: Union[str, None] = 'd5e7a8c89541'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('webhook_configs',
        sa.Column('phone_field', sa.String(length=100), nullable=True))
    op.add_column('webhook_configs',
        sa.Column('cta_mapping', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    # Backfill existing rows so SFA Company 1 keeps working without any manual config change
    op.execute("UPDATE webhook_configs SET phone_field = 'destinationPhoneNumber' WHERE phone_field IS NULL")


def downgrade() -> None:
    op.drop_column('webhook_configs', 'cta_mapping')
    op.drop_column('webhook_configs', 'phone_field')
