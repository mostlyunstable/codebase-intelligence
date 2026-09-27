"""Tenant isolation against a real Postgres, as a role without superuser or BYPASSRLS."""

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from codebase_intelligence.tenancy.access import (
    InsufficientRoleError,
    NotAMemberError,
    TenantContext,
    create_organization,
    get_repository,
    list_repositories,
    require_membership,
)
from codebase_intelligence.tenancy.models import (
    GitHubInstallation,
    MemberRole,
    Membership,
    Repository,
    User,
)

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class Tenant:
    owner_id: uuid.UUID
    org_id: uuid.UUID
    installation_id: uuid.UUID
    repository_id: uuid.UUID


async def _make_tenant(factory: SessionFactory, name: str, github_ids: int) -> Tenant:
    async with factory.begin() as session:
        user = User(email=f"{name}@example.com")
        session.add(user)
        await session.flush()
        ctx = await create_organization(session, owner_user_id=user.id, slug=name, name=name)
        installation = GitHubInstallation(
            org_id=ctx.org_id,
            github_installation_id=github_ids,
            account_login=name,
            account_type="Organization",
        )
        session.add(installation)
        await session.flush()
        repo = Repository(
            org_id=ctx.org_id,
            installation_id=installation.id,
            github_repo_id=github_ids,
            full_name=f"{name}/app",
            default_branch="main",
            is_private=True,
        )
        session.add(repo)
        await session.flush()
        return Tenant(user.id, ctx.org_id, installation.id, repo.id)


@pytest.fixture
async def tenants(session_factory: SessionFactory) -> tuple[Tenant, Tenant]:
    return (
        await _make_tenant(session_factory, "acme", 101),
        await _make_tenant(session_factory, "globex", 202),
    )


async def test_test_role_cannot_bypass_rls(session_factory: SessionFactory) -> None:
    # Guards every RLS assertion below: a superuser or BYPASSRLS role would make them vacuous.
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert tuple(row) == (False, False)


async def test_non_member_is_rejected_and_binding_cleared(
    session_factory: SessionFactory, tenants: tuple[Tenant, Tenant]
) -> None:
    acme, globex = tenants
    async with session_factory.begin() as session:
        with pytest.raises(NotAMemberError):
            await require_membership(session, user_id=acme.owner_id, org_id=globex.org_id)
        # Even if a caller swallows the error, the transaction must not stay bound to globex.
        visible = await session.scalars(select(Repository.id))
        assert list(visible) == []


async def test_unknown_org_and_foreign_org_are_indistinguishable(
    session_factory: SessionFactory, tenants: tuple[Tenant, Tenant]
) -> None:
    acme, globex = tenants
    errors = []
    for org_id in (globex.org_id, uuid.uuid4()):
        async with session_factory.begin() as session:
            with pytest.raises(NotAMemberError) as exc:
                await require_membership(session, user_id=acme.owner_id, org_id=org_id)
            errors.append(type(exc.value))
    assert errors == [NotAMemberError, NotAMemberError]


async def test_member_sees_only_own_org_repositories(
    session_factory: SessionFactory, tenants: tuple[Tenant, Tenant]
) -> None:
    acme, globex = tenants
    async with session_factory.begin() as session:
        ctx = await require_membership(session, user_id=acme.owner_id, org_id=acme.org_id)
        repos = await list_repositories(session, ctx)
        assert [r.id for r in repos] == [acme.repository_id]
        assert await get_repository(session, ctx, globex.repository_id) is None


async def test_forged_context_is_still_blocked_by_rls(
    session_factory: SessionFactory, tenants: tuple[Tenant, Tenant]
) -> None:
    """An application bug that builds a context for the wrong org must not expose data.

    The transaction is bound to acme, but the context claims globex. The explicit org filter
    and the RLS policy disagree, so nothing is returned.
    """
    acme, globex = tenants
    async with session_factory.begin() as session:
        await require_membership(session, user_id=acme.owner_id, org_id=acme.org_id)
        forged = TenantContext(user_id=acme.owner_id, org_id=globex.org_id, role=MemberRole.OWNER)
        assert await list_repositories(session, forged) == []
        assert await get_repository(session, forged, globex.repository_id) is None


async def test_rls_filters_unscoped_queries(
    session_factory: SessionFactory, tenants: tuple[Tenant, Tenant]
) -> None:
    acme, _ = tenants
    # No binding at all: fail closed.
    async with session_factory.begin() as session:
        assert list(await session.scalars(select(Repository.id))) == []
        assert list(await session.scalars(select(Membership.org_id))) == []
    # Bound to acme: a query with no WHERE clause still sees only acme's rows.
    async with session_factory.begin() as session:
        await require_membership(session, user_id=acme.owner_id, org_id=acme.org_id)
        assert list(await session.scalars(select(Repository.id))) == [acme.repository_id]
        assert list(await session.scalars(select(GitHubInstallation.org_id))) == [acme.org_id]


async def test_binding_does_not_outlive_transaction(
    session_factory: SessionFactory, tenants: tuple[Tenant, Tenant]
) -> None:
    acme, _ = tenants
    async with session_factory() as session:
        async with session.begin():
            await require_membership(session, user_id=acme.owner_id, org_id=acme.org_id)
        async with session.begin():
            assert list(await session.scalars(select(Repository.id))) == []


async def test_rls_rejects_writes_into_another_org(
    session_factory: SessionFactory, tenants: tuple[Tenant, Tenant]
) -> None:
    acme, globex = tenants
    with pytest.raises(DBAPIError, match="row-level security"):
        async with session_factory.begin() as session:
            await require_membership(session, user_id=acme.owner_id, org_id=acme.org_id)
            session.add(
                Repository(
                    org_id=globex.org_id,
                    installation_id=globex.installation_id,
                    github_repo_id=999,
                    full_name="globex/injected",
                    default_branch="main",
                    is_private=False,
                )
            )
            await session.flush()


async def test_repository_cannot_reference_another_orgs_installation(
    session_factory: SessionFactory, tenants: tuple[Tenant, Tenant]
) -> None:
    acme, globex = tenants
    with pytest.raises(IntegrityError, match="fk_repositories_installation_id_org_id"):
        async with session_factory.begin() as session:
            await require_membership(session, user_id=acme.owner_id, org_id=acme.org_id)
            session.add(
                Repository(
                    org_id=acme.org_id,
                    installation_id=globex.installation_id,
                    github_repo_id=303,
                    full_name="acme/cross-wired",
                    default_branch="main",
                    is_private=False,
                )
            )
            await session.flush()


async def test_role_requirement(
    session_factory: SessionFactory, tenants: tuple[Tenant, Tenant]
) -> None:
    acme, _ = tenants
    async with session_factory.begin() as session:
        member = User(email="member@example.com")
        session.add(member)
        await session.flush()
        await require_membership(session, user_id=acme.owner_id, org_id=acme.org_id)
        session.add(Membership(org_id=acme.org_id, user_id=member.id, role=MemberRole.MEMBER))
        member_id = member.id
    async with session_factory.begin() as session:
        ctx = await require_membership(session, user_id=member_id, org_id=acme.org_id)
        assert ctx.role is MemberRole.MEMBER
    async with session_factory.begin() as session:
        with pytest.raises(InsufficientRoleError):
            await require_membership(
                session, user_id=member_id, org_id=acme.org_id, min_role=MemberRole.ADMIN
            )
        assert list(await session.scalars(select(Repository.id))) == []
