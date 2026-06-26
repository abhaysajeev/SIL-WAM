"""new architecture: drop old order/survey/webhook tables, create conversation engine tables

Revision ID: a9b1c2d3e4f5
Revises: f5ec7e16b558
Create Date: 2026-06-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a9b1c2d3e4f5'
down_revision: Union[str, None] = 'f5ec7e16b558'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Drop old tables ───────────────────────────────────────────────────────

    # survey_questions first (FK to survey_profiles)
    op.drop_index('ix_survey_questions_profile_id', table_name='survey_questions')
    op.drop_table('survey_questions')

    op.drop_index('ix_survey_profiles_company_id', table_name='survey_profiles')
    op.drop_table('survey_profiles')

    # sales_orders (meta_message_id index was added in f5ec7e16b558)
    op.drop_index('ix_sales_orders_meta_message_id', table_name='sales_orders')
    op.drop_index('ix_sales_orders_status', table_name='sales_orders')
    op.drop_index('ix_sales_orders_destination_phone', table_name='sales_orders')
    op.drop_index('ix_sales_orders_company_id', table_name='sales_orders')
    op.drop_table('sales_orders')

    # webhook_configs
    op.drop_index('ix_webhook_configs_webhook_secret', table_name='webhook_configs')
    op.drop_index('ix_webhook_configs_company_id', table_name='webhook_configs')
    op.drop_table('webhook_configs')

    # ── Create new conversation engine tables ─────────────────────────────────

    op.create_table(
        'conversations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('company_id', sa.UUID(), nullable=False),
        sa.Column('mobile_no', sa.String(length=30), nullable=False),
        sa.Column('first_contact_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('last_activity_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('total_messages', sa.Integer(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('mobile_no', 'company_id', name='uq_conversation_mobile_company'),
    )
    op.create_index('ix_conversations_company_id', 'conversations', ['company_id'], unique=False)
    op.create_index('ix_conversations_mobile_no', 'conversations', ['mobile_no'], unique=False)

    op.create_table(
        'services',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        sa.Column('company_id', sa.UUID(), nullable=False),
        sa.Column('service_id', sa.String(length=200), nullable=False),
        sa.Column('template_id', sa.UUID(), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False, server_default='waiting'),
        sa.Column('expired_reason', sa.String(length=50), nullable=True),
        sa.Column('failed_reason', sa.String(length=100), nullable=True),
        sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('questions', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('template_expiry_hours', sa.Integer(), nullable=False, server_default='24'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['template_id'], ['whatsapp_templates.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('service_id', name='uq_services_service_id'),
    )
    op.create_index('ix_services_conversation_id', 'services', ['conversation_id'], unique=False)
    op.create_index('ix_services_company_id', 'services', ['company_id'], unique=False)
    op.create_index('ix_services_service_id', 'services', ['service_id'], unique=True)
    op.create_index('ix_services_status', 'services', ['status'], unique=False)

    op.create_table(
        'mobile_queue',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('company_id', sa.UUID(), nullable=False),
        sa.Column('mobile_no', sa.String(length=30), nullable=False),
        sa.Column('service_id', sa.UUID(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='waiting'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['service_id'], ['services.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mobile_queue_company_id', 'mobile_queue', ['company_id'], unique=False)
    op.create_index('ix_mobile_queue_status', 'mobile_queue', ['status'], unique=False)

    op.create_table(
        'service_responses',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('service_id', sa.UUID(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('field_key', sa.String(length=100), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('answer_type', sa.Integer(), nullable=False),
        sa.Column('response_value', sa.Text(), nullable=True),
        sa.Column('responded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['service_id'], ['services.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_service_responses_service_id', 'service_responses', ['service_id'], unique=False)

    op.create_table(
        'messages',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        sa.Column('service_id', sa.UUID(), nullable=True),
        sa.Column('wamid', sa.String(length=200), nullable=True),
        sa.Column('direction', sa.String(length=10), nullable=False),
        sa.Column('message_type', sa.String(length=30), nullable=False),
        sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('is_flow_message', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('status', sa.String(length=20), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['service_id'], ['services.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('wamid', name='uq_messages_wamid'),
    )
    op.create_index('ix_messages_conversation_id', 'messages', ['conversation_id'], unique=False)
    op.create_index('ix_messages_service_id', 'messages', ['service_id'], unique=False)
    op.create_index('ix_messages_wamid', 'messages', ['wamid'], unique=True)


def downgrade() -> None:
    # Drop new tables (reverse dependency order)
    op.drop_index('ix_messages_wamid', table_name='messages')
    op.drop_index('ix_messages_service_id', table_name='messages')
    op.drop_index('ix_messages_conversation_id', table_name='messages')
    op.drop_table('messages')

    op.drop_index('ix_service_responses_service_id', table_name='service_responses')
    op.drop_table('service_responses')

    op.drop_index('ix_mobile_queue_status', table_name='mobile_queue')
    op.drop_index('ix_mobile_queue_company_id', table_name='mobile_queue')
    op.drop_table('mobile_queue')

    op.drop_index('ix_services_status', table_name='services')
    op.drop_index('ix_services_service_id', table_name='services')
    op.drop_index('ix_services_company_id', table_name='services')
    op.drop_index('ix_services_conversation_id', table_name='services')
    op.drop_table('services')

    op.drop_index('ix_conversations_mobile_no', table_name='conversations')
    op.drop_index('ix_conversations_company_id', table_name='conversations')
    op.drop_table('conversations')

    # Restore old tables
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
        sa.Column('meta_message_id', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sales_orders_meta_message_id', 'sales_orders', ['meta_message_id'], unique=True)
    op.create_index('ix_sales_orders_status', 'sales_orders', ['status'], unique=False)
    op.create_index('ix_sales_orders_destination_phone', 'sales_orders', ['destination_phone'], unique=False)
    op.create_index('ix_sales_orders_company_id', 'sales_orders', ['company_id'], unique=False)

    op.create_table(
        'survey_profiles',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('company_id', sa.UUID(), nullable=False),
        sa.Column('external_profile_id', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'external_profile_id', name='uq_survey_profile_company_extid'),
    )
    op.create_index('ix_survey_profiles_company_id', 'survey_profiles', ['company_id'], unique=False)

    op.create_table(
        'survey_questions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('profile_id', sa.UUID(), nullable=False),
        sa.Column('external_question_id', sa.String(length=100), nullable=True),
        sa.Column('order_index', sa.Integer(), nullable=False),
        sa.Column('question_text', sa.Text(), nullable=False),
        sa.Column('answer_type', sa.String(length=20), nullable=False),
        sa.Column('button_options', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['profile_id'], ['survey_profiles.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('profile_id', 'order_index', name='uq_survey_question_profile_order'),
    )
    op.create_index('ix_survey_questions_profile_id', 'survey_questions', ['profile_id'], unique=False)
