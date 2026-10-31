"""grant the broadcast_settings page to the admin role

Revision ID: f2b5d8c03e49
Revises: e1a4c7b92d38
Create Date: 2026-09-07 10:14:51.226904

Same reason as c1f8a3d76e42 did for `agents`: the original role seed granted admin only
the pages that existed when it ran, so a resource added later is invisible to every
non-super-admin until a migration says otherwise.

This one is especially easy to miss because the test suite cannot catch it — tests
authenticate as super_admin, which bypasses require() entirely, so the page passes every
test and then 403s for the only people meant to use it.

Idempotent, so it is safe where someone has already ticked the box by hand.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'f2b5d8c03e49'
down_revision: Union[str, None] = 'e1a4c7b92d38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ADMIN_ROLE_ID = "00000000-0000-0000-0000-000000000002"


def upgrade() -> None:
    op.execute(f"""
        INSERT INTO role_page_permission
            (role_id, page_name, can_read, can_create, can_write, can_delete)
        VALUES ('{ADMIN_ROLE_ID}', 'broadcast_settings', TRUE, TRUE, TRUE, TRUE)
        ON CONFLICT (role_id, page_name) DO UPDATE
        SET can_read = TRUE, can_create = TRUE, can_write = TRUE, can_delete = TRUE
    """)


def downgrade() -> None:
    op.execute(f"""
        DELETE FROM role_page_permission
        WHERE role_id = '{ADMIN_ROLE_ID}' AND page_name = 'broadcast_settings'
    """)
