"""add user_id index on refresh_tokens

Revision ID: a7f3c1e9b204
Revises: 56e1524f310e
Create Date: 2026-06-29

Without this index, the per-user DELETE on login (cleanup of dead tokens)
and any user-scoped query on refresh_tokens do a full table scan.
"""
from typing import Union

from alembic import op

revision: str = 'a7f3c1e9b204'
down_revision: Union[str, None] = '56e1524f310e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_refresh_tokens_user_id', 'refresh_tokens', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_refresh_tokens_user_id', table_name='refresh_tokens')
