"""Multi-tenant domain model: Organization -> Memberships/Users, GitHub installations, Repositories.

Every org-scoped table carries a non-null ``org_id``. Tenant isolation is enforced in three
layers: explicit ``org_id`` filters in the service layer, database constraints that keep
related rows inside one org, and Postgres row-level security (see migration 0001).
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from codebase_intelligence.db.base import Base


class MemberRole(enum.StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


# Higher rank grants a superset of the permissions of lower ranks.
ROLE_RANK: dict[MemberRole, int] = {MemberRole.MEMBER: 0, MemberRole.ADMIN: 1, MemberRole.OWNER: 2}


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$'", name="slug_format"),
    )


class User(Base):
    """A global identity. Users belong to organizations only through ``Membership``."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Stored lower-cased; the check constraint makes the unique constraint case-insensitive.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)


class Membership(Base):
    __tablename__ = "memberships"

    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[MemberRole] = mapped_column(
        Enum(
            MemberRole,
            name="member_role",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = _created_at()


class GitHubInstallation(Base):
    __tablename__ = "github_installations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # A GitHub App installation id is globally unique and may be bound to exactly one org.
    github_installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    account_login: Mapped[str] = mapped_column(String(100), nullable=False)
    account_type: Mapped[str] = mapped_column(String(16), nullable=False)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("account_type IN ('User', 'Organization')", name="account_type"),
        # Target of the composite FK from repositories: lets the DB prove a repository and its
        # installation belong to the same org.
        UniqueConstraint("id", "org_id"),
    )


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    installation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    github_repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    is_private: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["installation_id", "org_id"],
            ["github_installations.id", "github_installations.org_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("org_id", "github_repo_id"),
    )
