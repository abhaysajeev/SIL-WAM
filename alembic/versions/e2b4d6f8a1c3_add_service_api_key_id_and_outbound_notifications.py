"""add_service_api_key_id_and_outbound_notifications

Revision ID: e2b4d6f8a1c3
Revises: 35d829413d8b
Create Date: 2026-07-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e2b4d6f8a1c3'
down_revision: Union[str, None] = '35d829413d8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'services',
        sa.Column('api_key_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_services_api_key_id', 'services', 'company_api_keys',
        ['api_key_id'], ['id'], ondelete='SET NULL',
    )
    op.create_index('ix_services_api_key_id', 'services', ['api_key_id'])

    op.create_table(
        'outbound_notifications',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('service_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('message_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('notify_url', sa.String(length=500), nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['service_id'], ['services.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_outbound_notifications_service_id', 'outbound_notifications', ['service_id'])
    op.create_index('ix_outbound_notifications_message_id', 'outbound_notifications', ['message_id'])
    op.create_index('ix_outbound_notifications_status', 'outbound_notifications', ['status'])
    op.create_index('ix_outbound_notifications_next_attempt_at', 'outbound_notifications', ['next_attempt_at'])


def downgrade() -> None:
    op.drop_table('outbound_notifications')
    op.drop_index('ix_services_api_key_id', table_name='services')
    op.drop_constraint('fk_services_api_key_id', 'services', type_='foreignkey')
    op.drop_column('services', 'api_key_id')
