"""add webhook_config and sales_orders

Revision ID: d5e7a8c89541
Revises: e5f6a7b8c9d0
Create Date: 2026-06-09 07:41:09.781122

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'd5e7a8c89541'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sales_orders',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('company_id', sa.UUID(), nullable=True),
        sa.Column('source_phone', sa.String(length=50), nullable=False),
        sa.Column('destination_phone', sa.String(length=50), nullable=False),
        sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('template_params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sales_orders_company_id', 'sales_orders', ['company_id'], unique=False)
    op.create_index('ix_sales_orders_destination_phone', 'sales_orders', ['destination_phone'], unique=False)
    op.create_index('ix_sales_orders_status', 'sales_orders', ['status'], unique=False)

    op.create_table(
        'webhook_configs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('company_id', sa.UUID(), nullable=False),
        sa.Column('template_id', sa.UUID(), nullable=True),
        sa.Column('param_mapping', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('webhook_secret', sa.String(length=128), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['template_id'], ['whatsapp_templates.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', name='uq_webhook_configs_company_id'),
        sa.UniqueConstraint('webhook_secret', name='uq_webhook_configs_secret'),
    )
    op.create_index('ix_webhook_configs_company_id', 'webhook_configs', ['company_id'], unique=True)
    op.create_index('ix_webhook_configs_webhook_secret', 'webhook_configs', ['webhook_secret'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_webhook_configs_webhook_secret', table_name='webhook_configs')
    op.drop_index('ix_webhook_configs_company_id', table_name='webhook_configs')
    op.drop_table('webhook_configs')
    op.drop_index('ix_sales_orders_status', table_name='sales_orders')
    op.drop_index('ix_sales_orders_destination_phone', table_name='sales_orders')
    op.drop_index('ix_sales_orders_company_id', table_name='sales_orders')
    op.drop_table('sales_orders')
