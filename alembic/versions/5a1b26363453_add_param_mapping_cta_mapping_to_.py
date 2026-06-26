"""add_param_mapping_cta_mapping_to_templates

Revision ID: 5a1b26363453
Revises: c9bc50e5e725
Create Date: 2026-06-16 23:08:51.169992

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '5a1b26363453'
down_revision: Union[str, None] = 'c9bc50e5e725'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('whatsapp_templates', sa.Column('param_mapping', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('whatsapp_templates', sa.Column('cta_mapping', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('whatsapp_templates', 'cta_mapping')
    op.drop_column('whatsapp_templates', 'param_mapping')
