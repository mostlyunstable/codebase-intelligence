"""Migrations run for real against Postgres, as the non-superuser application role."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from codebase_intelligence.db.base import Base
from codebase_intelligence.db.session import create_engine
from codebase_intelligence.tenancy import models  # noqa: F401  (registers tables)

from .conftest import recreate_database, run_alembic

TENANT_TABLES = {"organizations", "memberships", "github_installations", "repositories"}


async def test_upgrade_downgrade_upgrade_round_trip() -> None:
    url = await recreate_database("ci_test_migrations")
    run_alembic(url, "upgrade", "head")
    run_alembic(url, "downgrade", "base")
    run_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    try:
        async with eng.connect() as conn:
            version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        assert version == "0001"
    finally:
        await eng.dispose()


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    def diff(conn: Connection) -> list[object]:
        return list(compare_metadata(MigrationContext.configure(conn), Base.metadata))

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []


async def test_rls_enabled_and_forced_on_every_tenant_table(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = ANY(:names)"
                ),
                {"names": list(TENANT_TABLES)},
            )
        ).all()
    assert {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows} == {
        t: (True, True) for t in TENANT_TABLES
    }
