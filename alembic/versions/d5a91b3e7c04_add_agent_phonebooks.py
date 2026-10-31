"""add agent_phonebooks mapping

Revision ID: d5a91b3e7c04
Revises: c1f8a3d76e42
Create Date: 2026-09-03 12:04:37.610284

Mapping only — which phonebooks an agent may work from. Contacts stay in
phonebook_contacts and are read live, so an import or an edit reaches the app on its
next call rather than needing anything copied here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5a91b3e7c04'
down_revision: Union[str, None] = 'c1f8a3d76e42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_phonebooks",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phonebook_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        # CASCADE both ways: a mapping to a phonebook that no longer exists is not worth
        # keeping, and a nullable phonebook_id would need a guard at every read site.
        sa.ForeignKeyConstraint(["phonebook_id"], ["phonebooks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "phonebook_id", name="uq_agent_phonebook"),
    )
    op.create_index("ix_agent_phonebooks_agent_id", "agent_phonebooks", ["agent_id"])
    op.create_index("ix_agent_phonebooks_phonebook_id", "agent_phonebooks", ["phonebook_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_phonebooks_phonebook_id", table_name="agent_phonebooks")
    op.drop_index("ix_agent_phonebooks_agent_id", table_name="agent_phonebooks")
    op.drop_table("agent_phonebooks")
