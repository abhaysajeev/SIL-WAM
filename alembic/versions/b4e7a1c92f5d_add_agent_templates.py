"""add agent_templates mapping

Revision ID: b4e7a1c92f5d
Revises: 9d2e5c81b47a
Create Date: 2026-09-02 15:02:11.447301

Mapping only — which templates an agent may send. The structure (body, parameters,
header) is deliberately NOT copied here; it is read live from whatsapp_templates on
every request, because Meta rewrites `components` on every sync and a snapshot would
drift into disagreeing with what Meta actually accepts.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4e7a1c92f5d'
down_revision: Union[str, None] = '9d2e5c81b47a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_templates",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        # CASCADE, not SET NULL: a mapping to a template that no longer exists is not a
        # record worth keeping, and a nullable template_id would need a guard at every
        # read site.
        sa.ForeignKeyConstraint(["template_id"], ["whatsapp_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "template_id", name="uq_agent_template"),
    )
    op.create_index("ix_agent_templates_agent_id", "agent_templates", ["agent_id"])
    op.create_index("ix_agent_templates_template_id", "agent_templates", ["template_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_templates_template_id", table_name="agent_templates")
    op.drop_index("ix_agent_templates_agent_id", table_name="agent_templates")
    op.drop_table("agent_templates")
