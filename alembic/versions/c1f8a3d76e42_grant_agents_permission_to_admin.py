"""grant the agents page to the admin role

Revision ID: c1f8a3d76e42
Revises: b4e7a1c92f5d
Create Date: 2026-09-03 11:20:44.802115

The `agents` resource was added to RESOURCES after a1b2c3d4e5f6 seeded the roles, and
that migration granted admin only the pages that existed when it ran. The result is that
`require("agents", …)` refuses every non-super-admin — the Agent Config tab loads, shows
nothing, and saves nothing, because super_admin bypasses the guard and so nobody notices
during development.

Restores the intent of the original seed (admin holds every page) rather than widening
it. Idempotent, so it is safe on a database where someone has already ticked the box.

NOTE: admin is also missing campaigns, conversations, erpnext_configs, phonebooks and
services for the same reason. Those are deliberately NOT granted here — each is a
separate decision about who may send broadcasts or read customer conversations, and this
migration exists to fix the agents feature, not to quietly hand out five others.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c1f8a3d76e42'
down_revision: Union[str, None] = 'b4e7a1c92f5d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ADMIN_ROLE_ID = "00000000-0000-0000-0000-000000000002"


def upgrade() -> None:
    op.execute(f"""
        INSERT INTO role_page_permission
            (role_id, page_name, can_read, can_create, can_write, can_delete)
        VALUES ('{ADMIN_ROLE_ID}', 'agents', TRUE, TRUE, TRUE, TRUE)
        ON CONFLICT (role_id, page_name) DO UPDATE
        SET can_read = TRUE, can_create = TRUE, can_write = TRUE, can_delete = TRUE
    """)


def downgrade() -> None:
    op.execute(f"""
        DELETE FROM role_page_permission
        WHERE role_id = '{ADMIN_ROLE_ID}' AND page_name = 'agents'
    """)
