"""add roles, companies, role_page_permission; overhaul users

Revision ID: a1b2c3d4e5f6
Revises: 4f4aaf544cb6
Create Date: 2026-06-04 12:00:00.000000

"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "4f4aaf544cb6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── Seed UUIDs (fixed so downgrade can reference them) ────────────────────────
SUPER_ADMIN_ID = "00000000-0000-0000-0000-000000000001"
ADMIN_ID       = "00000000-0000-0000-0000-000000000002"
CV_ID          = "00000000-0000-0000-0000-000000000003"  # company_viewer

ALL_PAGES = [
    "dashboard", "templates", "promotional", "transactional",
    "companies", "users", "reports",
]
ADMIN_PAGES = ALL_PAGES  # admin gets all pages, all actions
COMPANY_VIEWER_PAGES = ["dashboard", "reports"]  # read only


def upgrade() -> None:
    # ── 1. companies ─────────────────────────────────────────────────────────
    op.create_table(
        "companies",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("company_code", sa.String(50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_code"),
    )
    op.create_index("ix_companies_company_code", "companies", ["company_code"], unique=True)

    # ── 2. roles ─────────────────────────────────────────────────────────────
    op.create_table(
        "roles",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_roles_name", "roles", ["name"], unique=True)

    # ── 3. role_page_permission ───────────────────────────────────────────────
    op.create_table(
        "role_page_permission",
        sa.Column("role_id", UUID(as_uuid=True), nullable=False),
        sa.Column("page_name", sa.String(50), nullable=False),
        sa.Column("can_read",   sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_create", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_write",  sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_delete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "page_name"),
    )

    # ── 4. alter users — add new columns ─────────────────────────────────────
    op.add_column("users", sa.Column("full_name", sa.String(200), nullable=True))
    op.add_column("users", sa.Column("phone", sa.String(20), nullable=True))
    op.add_column("users", sa.Column("role_id", UUID(as_uuid=True), nullable=True))
    op.add_column("users", sa.Column("company_id", UUID(as_uuid=True), nullable=True))
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("users", sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("created_by_id", UUID(as_uuid=True), nullable=True))

    # back-fill full_name from username so NOT NULL constraint is satisfiable
    op.execute("UPDATE users SET full_name = username WHERE full_name IS NULL")
    op.alter_column("users", "full_name", nullable=False)

    # foreign keys on users
    op.create_foreign_key("fk_users_role_id",       "users", "roles",     ["role_id"],       ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_users_company_id",    "users", "companies", ["company_id"],    ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_users_created_by_id", "users", "users",     ["created_by_id"], ["id"], ondelete="SET NULL")

    op.create_index("ix_users_role_id",    "users", ["role_id"])
    op.create_index("ix_users_company_id", "users", ["company_id"])

    # ── 5. drop old role string column ────────────────────────────────────────
    op.drop_column("users", "role")

    # ── 6. seed roles ─────────────────────────────────────────────────────────
    op.execute(f"""
        INSERT INTO roles (id, name, display_name, description, is_system)
        VALUES
          ('{SUPER_ADMIN_ID}', 'super_admin',     'Super Admin',       'Full system access — bypass all permission checks', true),
          ('{ADMIN_ID}',       'admin',            'Administrator',     'Manages companies and users; permissions configurable by super admin', false),
          ('{CV_ID}',          'company_viewer',   'Company Viewer',    'Read-only access to company dashboard and reports', false)
    """)

    # ── 7. seed permissions ───────────────────────────────────────────────────
    # admin: all pages, all actions
    admin_rows = ", ".join(
        f"('{ADMIN_ID}', '{page}', true, true, true, true)"
        for page in ADMIN_PAGES
    )
    op.execute(f"""
        INSERT INTO role_page_permission (role_id, page_name, can_read, can_create, can_write, can_delete)
        VALUES {admin_rows}
    """)

    # company_viewer: dashboard + reports, read only
    cv_rows = ", ".join(
        f"('{CV_ID}', '{page}', true, false, false, false)"
        for page in COMPANY_VIEWER_PAGES
    )
    op.execute(f"""
        INSERT INTO role_page_permission (role_id, page_name, can_read, can_create, can_write, can_delete)
        VALUES {cv_rows}
    """)


def downgrade() -> None:
    # restore role column before dropping FKs that reference roles table
    op.add_column("users", sa.Column("role", sa.String(50), nullable=True))
    op.execute("UPDATE users SET role = 'user'")
    op.alter_column("users", "role", nullable=False)

    # remove user FKs + columns
    op.drop_constraint("fk_users_created_by_id", "users", type_="foreignkey")
    op.drop_constraint("fk_users_company_id",    "users", type_="foreignkey")
    op.drop_constraint("fk_users_role_id",       "users", type_="foreignkey")

    op.drop_index("ix_users_company_id", table_name="users")
    op.drop_index("ix_users_role_id",    table_name="users")

    op.drop_column("users", "created_by_id")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_attempts")
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "company_id")
    op.drop_column("users", "role_id")
    op.drop_column("users", "phone")
    op.drop_column("users", "full_name")

    # drop new tables
    op.drop_table("role_page_permission")
    op.drop_index("ix_roles_name",            table_name="roles")
    op.drop_table("roles")
    op.drop_index("ix_companies_company_code", table_name="companies")
    op.drop_table("companies")
