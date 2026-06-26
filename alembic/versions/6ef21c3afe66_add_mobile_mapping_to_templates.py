"""add_mobile_mapping_to_templates

Revision ID: 6ef21c3afe66
Revises: 8ad9108c6bb1
Create Date: 2026-06-17 12:21:00.020057

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '6ef21c3afe66'
down_revision: Union[str, None] = '8ad9108c6bb1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('whatsapp_templates', sa.Column('mobile_mapping', sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column('whatsapp_templates', 'mobile_mapping')
