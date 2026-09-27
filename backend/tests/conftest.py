"""Test fixtures backed by a real Postgres server.

A superuser connection (CI_TEST_ADMIN_DATABASE_URL) is used only to create a dedicated
non-superuser role without BYPASSRLS and throwaway databases owned by it. Migrations and all
test queries run as that role, so row-level security is actually enforced in tests.
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from codebase_intelligence.db.session import create_engine, create_session_factory

BACKEND_DIR = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get(
    "CI_TEST_ADMIN_DATABASE_URL", "postgresql://postgres@localhost:5432/postgres"
)
APP_ROLE = "ci_test_app"
APP_PASSWORD = "ci_test_app"  # local throwaway role, not a real credential


def app_database_url(db_name: str) -> str:
    admin = urlsplit(ADMIN_URL)
    host = admin.hostname or "localhost"
    port = admin.port or 5432
    return f"postgresql+asyncpg://{APP_ROLE}:{APP_PASSWORD}@{host}:{port}/{db_name}"


async def recreate_database(db_name: str) -> str:
    conn = await asyncpg.connect(ADMIN_URL)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", APP_ROLE)
        if not exists:
            await conn.execute(
                f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{APP_PASSWORD}'"
            )
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{db_name}" OWNER {APP_ROLE}')
    finally:
        await conn.close()
    return app_database_url(db_name)


def run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env={**os.environ, "CI_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr}"
    return result


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    url = await recreate_database("ci_test")
    run_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture(autouse=True)
async def _clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    yield
    # TRUNCATE is not subject to RLS, so the owning role can clear every tenant's rows.
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE repositories, github_installations, memberships, users, organizations")
        )
