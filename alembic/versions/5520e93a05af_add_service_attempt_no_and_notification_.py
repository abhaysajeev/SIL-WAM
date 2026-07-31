"""add service attempt_no and notification service_attempt

Lets a retried service replay its full status lifecycle to the client.

notify_queue enforces monotonic status progression by ranking each new status
against the highest already sent for that service. A service that failed has a
terminal "failed" (rank 6) on record, so after a retry every status of the new
attempt ranks <= 6 and is suppressed — including the new "completed", also rank 6.
The client was therefore never told the corrected number succeeded.

services.attempt_no increments on each client retry; outbound_notifications
.service_attempt records which attempt a notification belongs to, so the rank
comparison happens within one attempt instead of across the service's lifetime.

NOTE: `alembic revision --autogenerate` additionally proposed dropping ten
unrelated constraints and indexes (uq_messages_wamid, uq_whatsapp_templates_
company_name_lang, companies_company_code_key, roles_name_key and others) that
exist in the database but are not declared on the models. That is pre-existing
model/DB drift, not part of this change, and dropping uq_messages_wamid in
particular would remove the inbound-webhook dedup guarantee that
conversation_engine depends on — Redis is optional and not deployed, so that
unique index is the only thing preventing duplicate message processing. All of
it has been removed from this migration deliberately. The drift should be
reconciled separately and on purpose.

Revision ID: 5520e93a05af
Revises: 41430e0b1cd8
Create Date: 2026-07-31 10:40:54.805987

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5520e93a05af'
down_revision: Union[str, None] = '41430e0b1cd8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows are the original attempt — server_default 0 backfills them.
    op.add_column(
        'services',
        sa.Column('attempt_no', sa.Integer(), server_default='0', nullable=False),
    )
    op.add_column(
        'outbound_notifications',
        sa.Column('service_attempt', sa.Integer(), server_default='0', nullable=False),
    )
    # Indexed because _max_notified_rank filters on (service_id, service_attempt)
    # on every single enqueue.
    op.create_index(
        op.f('ix_outbound_notifications_service_attempt'),
        'outbound_notifications',
        ['service_attempt'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_outbound_notifications_service_attempt'),
        table_name='outbound_notifications',
    )
    op.drop_column('outbound_notifications', 'service_attempt')
    op.drop_column('services', 'attempt_no')
