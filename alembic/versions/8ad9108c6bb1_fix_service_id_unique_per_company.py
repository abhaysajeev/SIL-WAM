"""fix_service_id_unique_per_company

Revision ID: 8ad9108c6bb1
Revises: 5a1b26363453
Create Date: 2026-06-17 11:57:06.502710

"""
from typing import Sequence, Union

from alembic import op

revision: str = '8ad9108c6bb1'
down_revision: Union[str, None] = '5a1b26363453'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Replace global unique on service_id with per-company unique
    op.drop_constraint('uq_services_service_id', 'services', type_='unique')
    op.drop_index('ix_services_service_id', table_name='services')
    op.create_index('ix_services_service_id', 'services', ['service_id'], unique=False)
    op.create_unique_constraint('uq_service_id_company', 'services', ['service_id', 'company_id'])


def downgrade() -> None:
    op.drop_constraint('uq_service_id_company', 'services', type_='unique')
    op.drop_index('ix_services_service_id', table_name='services')
    op.create_index('ix_services_service_id', 'services', ['service_id'], unique=True)
    op.create_unique_constraint('uq_services_service_id', 'services', ['service_id'])
