"""add_erpnext_config_and_wamid

Revision ID: f5ec7e16b558
Revises: 56e1524f310e
Create Date: 2026-06-15 12:20:59.509470

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f5ec7e16b558'
down_revision: Union[str, None] = '56e1524f310e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'erpnext_configs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('company_id', sa.UUID(), nullable=False),
        sa.Column('base_url', sa.String(length=500), nullable=False),
        sa.Column('api_key', sa.String(length=200), nullable=False),
        sa.Column('api_secret', sa.String(length=200), nullable=False),
        sa.Column('pdf_method', sa.String(length=300), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id'),
    )
    op.create_index('ix_erpnext_configs_company_id', 'erpnext_configs', ['company_id'], unique=True)

    op.add_column('sales_orders', sa.Column('meta_message_id', sa.String(length=128), nullable=True))
    op.create_index('ix_sales_orders_meta_message_id', 'sales_orders', ['meta_message_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_sales_orders_meta_message_id', table_name='sales_orders')
    op.drop_column('sales_orders', 'meta_message_id')

    op.drop_index('ix_erpnext_configs_company_id', table_name='erpnext_configs')
    op.drop_table('erpnext_configs')
