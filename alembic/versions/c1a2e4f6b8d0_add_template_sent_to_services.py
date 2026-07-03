"""add_template_sent_to_services

Revision ID: c1a2e4f6b8d0
Revises: 6ef21c3afe66
Create Date: 2026-07-02 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c1a2e4f6b8d0'
down_revision: Union[str, None] = '6ef21c3afe66'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'services',
        sa.Column('template_sent', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index('ix_services_template_sent', 'services', ['template_sent'])


def downgrade() -> None:
    op.drop_index('ix_services_template_sent', table_name='services')
    op.drop_column('services', 'template_sent')
