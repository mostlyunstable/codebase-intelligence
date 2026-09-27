"""Tenant binding and org-membership checks.

All org-scoped work happens inside a transaction that has first been bound to one org with
``require_membership`` (or ``create_organization``). Binding sets the transaction-local
``app.current_org_id`` setting that the row-level-security policies read, so it is reset
automatically at commit/rollback and cannot leak across pooled connections.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from codebase_intelligence.tenancy.models import (
    ROLE_RANK,
    MemberRole,
    Membership,
    Organization,
    Repository,
)

TENANT_SETTING = "app.current_org_id"


class TenantAccessError(Exception):
    """Base class for tenant-access failures."""


class NotAMemberError(TenantAccessError):
    """The user is not a member of the org, or the org does not exist.

    Both cases raise the same error on purpose so callers cannot probe for other tenants' orgs.
    """

    def __init__(self, org_id: uuid.UUID) -> None:
        super().__init__(f"no access to organization {org_id}")
        self.org_id = org_id


class InsufficientRoleError(TenantAccessError):
    def __init__(self, org_id: uuid.UUID, required: MemberRole, actual: MemberRole) -> None:
        super().__init__(f"role {actual} is below required {required} in organization {org_id}")
        self.org_id = org_id
        self.required = required
        self.actual = actual


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Proof that ``user_id`` holds ``role`` in ``org_id`` within the current transaction."""

    user_id: uuid.UUID
    org_id: uuid.UUID
    role: MemberRole


async def _set_bound_org(session: AsyncSession, org_id: uuid.UUID | None) -> None:
    # is_local=true scopes the setting to the current transaction.
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": TENANT_SETTING, "value": "" if org_id is None else str(org_id)},
    )


async def require_membership(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    min_role: MemberRole = MemberRole.MEMBER,
) -> TenantContext:
    """Bind the transaction to ``org_id`` if ``user_id`` is a member with at least ``min_role``.

    On failure the binding is cleared before raising, so a caller that swallows the error
    cannot go on to read the other org's rows.
    """
    await _set_bound_org(session, org_id)
    role = await session.scalar(
        select(Membership.role).where(Membership.org_id == org_id, Membership.user_id == user_id)
    )
    if role is None:
        await _set_bound_org(session, None)
        raise NotAMemberError(org_id)
    if ROLE_RANK[role] < ROLE_RANK[min_role]:
        await _set_bound_org(session, None)
        raise InsufficientRoleError(org_id, min_role, role)
    return TenantContext(user_id=user_id, org_id=org_id, role=role)


async def create_organization(
    session: AsyncSession, *, owner_user_id: uuid.UUID, slug: str, name: str
) -> TenantContext:
    """Create an org with ``owner_user_id`` as owner and bind the transaction to it."""
    org_id = uuid.uuid4()
    await _set_bound_org(session, org_id)
    session.add(Organization(id=org_id, slug=slug, name=name))
    await session.flush()
    session.add(Membership(org_id=org_id, user_id=owner_user_id, role=MemberRole.OWNER))
    await session.flush()
    return TenantContext(user_id=owner_user_id, org_id=org_id, role=MemberRole.OWNER)


async def list_repositories(session: AsyncSession, ctx: TenantContext) -> list[Repository]:
    result = await session.scalars(
        select(Repository).where(Repository.org_id == ctx.org_id).order_by(Repository.full_name)
    )
    return list(result)


async def get_repository(
    session: AsyncSession, ctx: TenantContext, repository_id: uuid.UUID
) -> Repository | None:
    """Return the repository only if it belongs to ``ctx``'s org; otherwise ``None``."""
    return await session.scalar(
        select(Repository).where(Repository.id == repository_id, Repository.org_id == ctx.org_id)
    )
