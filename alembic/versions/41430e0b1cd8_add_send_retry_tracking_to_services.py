"""add send retry tracking to services

Revision ID: 41430e0b1cd8
Revises: e2b4d6f8a1c3
Create Date: 2026-07-13 15:17:48.493427

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '41430e0b1cd8'
down_revision: Union[str, None] = 'e2b4d6f8a1c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Note: autogenerate also picked up unrelated pre-existing constraint/index
    # naming drift on other tables (companies, conversations, erpnext_configs,
    # messages, refresh_tokens, roles, whatsapp_accounts, whatsapp_onboarding_sessions,
    # whatsapp_templates) — intentionally excluded here, out of scope for this change.
    op.add_column('services', sa.Column('send_attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('services', sa.Column('next_retry_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_services_next_retry_at'), 'services', ['next_retry_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_services_next_retry_at'), table_name='services')
    op.drop_column('services', 'next_retry_at')
    op.drop_column('services', 'send_attempts')
