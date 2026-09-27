# Codebase Intelligence

Evidence-driven AI software engineering agent (multi-tenant SaaS). This repository is a
modular monolith; see [ADR 0001](docs/adr/0001-modular-monolith-async-postgres-multitenancy.md).

```
backend/    Python 3.12, FastAPI, SQLAlchemy 2.x (async), Alembic, uv
frontend/   Next.js, TypeScript
docs/adr/   Architecture decision records
```

Current scope: phase 0/1 (tenancy foundation). No GitHub integration, ingestion, or
retrieval yet.

## Backend

Requires Postgres 16 and [uv](https://docs.astral.sh/uv/).

```sh
cd backend
uv sync
# Tests create a non-superuser role and throwaway databases through this superuser URL:
export CI_TEST_ADMIN_DATABASE_URL=postgresql://postgres@localhost:5432/postgres
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Migrations read `CI_DATABASE_URL` (`postgresql+asyncpg://...`). Connect as a role that is
not a superuser and lacks `BYPASSRLS`, or row-level security will not apply:

```sh
CI_DATABASE_URL=postgresql+asyncpg://app:***@host:5432/db uv run alembic upgrade head
```

## Frontend

```sh
cd frontend
npm ci
npm run typecheck && npm run build
```
