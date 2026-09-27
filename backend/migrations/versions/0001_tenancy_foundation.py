"""Tenancy foundation: organizations, users, memberships, GitHub installations, repositories.

Revision ID: 0001
Revises:
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Org-scoped tables and the column that names the owning org.
_TENANT_TABLES = {
    "organizations": "id",
    "memberships": "org_id",
    "github_installations": "org_id",
    "repositories": "org_id",
}


def _uuid_pk() -> sa.Column[postgresql.UUID]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _created_at() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "organizations",
        _uuid_pk(),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$'", name=op.f("ck_organizations_slug_format")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("slug", name=op.f("uq_organizations_slug")),
    )
    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        _created_at(),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lowercase")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "memberships",
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner",
                "admin",
                "member",
                name="member_role",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            ondelete="CASCADE",
            name=op.f("fk_memberships_org_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name=op.f("fk_memberships_user_id_users")
        ),
        sa.PrimaryKeyConstraint("org_id", "user_id", name=op.f("pk_memberships")),
    )
    op.create_index(op.f("ix_memberships_user_id"), "memberships", ["user_id"])
    op.create_table(
        "github_installations",
        _uuid_pk(),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.String(100), nullable=False),
        sa.Column("account_type", sa.String(16), nullable=False),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "account_type IN ('User', 'Organization')",
            name=op.f("ck_github_installations_account_type"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            ondelete="CASCADE",
            name=op.f("fk_github_installations_org_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_installations")),
        sa.UniqueConstraint(
            "github_installation_id", name=op.f("uq_github_installations_github_installation_id")
        ),
        sa.UniqueConstraint("id", "org_id", name=op.f("uq_github_installations_id_org_id")),
    )
    op.create_index(op.f("ix_github_installations_org_id"), "github_installations", ["org_id"])
    op.create_table(
        "repositories",
        _uuid_pk(),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("github_repo_id", sa.BigInteger(), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("default_branch", sa.String(255), nullable=False),
        sa.Column("is_private", sa.Boolean(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["installation_id", "org_id"],
            ["github_installations.id", "github_installations.org_id"],
            ondelete="CASCADE",
            name=op.f("fk_repositories_installation_id_org_id_github_installations"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            ondelete="CASCADE",
            name=op.f("fk_repositories_org_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repositories")),
        sa.UniqueConstraint(
            "org_id", "github_repo_id", name=op.f("uq_repositories_org_id_github_repo_id")
        ),
    )
    op.create_index(op.f("ix_repositories_org_id"), "repositories", ["org_id"])

    # Row-level security. The bound org comes from the transaction-local setting written by
    # codebase_intelligence.tenancy.access; unset or empty means "no org" and matches no rows.
    op.execute(
        """
        CREATE FUNCTION app_current_org_id() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$ SELECT nullif(current_setting('app.current_org_id', true), '')::uuid $$
        """
    )
    for table, column in _TENANT_TABLES.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE applies the policy to the table owner too (superusers/BYPASSRLS still bypass).
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({column} = app_current_org_id()) "
            f"WITH CHECK ({column} = app_current_org_id())"
        )


def downgrade() -> None:
    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("repositories")
    op.drop_table("github_installations")
    op.drop_table("memberships")
    op.drop_table("users")
    op.drop_table("organizations")
    op.execute("DROP FUNCTION app_current_org_id()")
