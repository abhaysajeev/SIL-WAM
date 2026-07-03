"""merge_refresh_token_index_and_template_sent

Revision ID: 35d829413d8b
Revises: a7f3c1e9b204, c1a2e4f6b8d0
Create Date: 2026-07-02 11:40:01.781618

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '35d829413d8b'
down_revision: Union[str, None] = ('a7f3c1e9b204', 'c1a2e4f6b8d0')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
