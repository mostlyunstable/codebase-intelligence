# ADR 0001: Modular monolith, async Postgres, and the multi-tenant hierarchy

- Status: Proposed
- Date: 2026-09-27

## Context

Codebase Intelligence is a multi-tenant SaaS targeting roughly 1,000 users across many
organizations and repositories. Every later phase (GitHub integration, repository sync, code
knowledge, retrieval, agent runs) stores data that belongs to exactly one organization, so
the tenancy model and its enforcement have to exist before any of that data does. Leaking
one tenant's code or findings to another is the most damaging failure the product can have.

The team is small and the load is modest. We need fast iteration, a single deployable, and
a database that can hold relational data, full-text search, and vectors (pgvector) together.
Supabase is the intended Postgres host, but the domain must not depend on Supabase-only APIs.

## Decision

1. **Modular monolith.** One repository with `backend/` (Python 3.12, FastAPI, Pydantic v2,
   SQLAlchemy 2.x async, Alembic, managed with uv) and `frontend/` (Next.js, TypeScript).
   Backend modules are Python packages under `codebase_intelligence/` (`tenancy`, `db`,
   `api`, later `github`, `ingestion`, ...) that talk through function calls, not the network.
2. **Async Postgres via SQLAlchemy + asyncpg**, schema owned by Alembic migrations in this
   repo. Any Postgres 16 works; Supabase is treated as a hosted Postgres, nothing more.
3. **Tenant hierarchy.** `Organization` is the tenant. `User` is a global identity that joins
   orgs through `Membership(org_id, user_id, role)` with roles owner > admin > member.
   `GitHubInstallation` and `Repository` belong to exactly one org. Future org-scoped entities
   (conversations, agent runs, usage) follow the same pattern: a non-null `org_id`.
4. **Isolation is enforced in three independent layers:**
   - *Application:* `require_membership` proves membership and role, returns a
     `TenantContext`, and every org-scoped query filters on `ctx.org_id`. Unknown and foreign
     orgs raise the same error so tenants cannot be probed.
   - *Database constraints:* related rows must share an org, e.g. a repository's
     `(installation_id, org_id)` is a composite foreign key to its installation.
   - *Row-level security:* every org-scoped table has `ENABLE` + `FORCE ROW LEVEL SECURITY`
     and a policy comparing its org column to `app_current_org_id()`, which reads the
     transaction-local setting `app.current_org_id`. Unset means no rows (fail closed).
     The setting is written with `set_config(..., is_local => true)`, so it ends with the
     transaction and cannot leak across pooled connections.

## Alternatives considered

- **Microservices from the start.** Rejected: operational cost (deploys, network failures,
  distributed transactions, tracing) with no measured scaling need at this size.
- **Schema-per-tenant or database-per-tenant.** Stronger physical isolation, but migrations
  and connection counts grow with tenant count, and cross-tenant operations (billing,
  admin) get harder. Revisit only if a customer contract requires it.
- **Application-only filtering (no RLS).** Simpler, but one missing `WHERE org_id = ...`
  becomes a cross-tenant leak. RLS turns that bug into an empty result.
- **Supabase `auth.uid()`-based RLS and PostgREST.** Couples the domain to Supabase Auth
  and bypasses the backend's authorization logic. The setting-based policy works on any
  Postgres, including Supabase.
- **Sync SQLAlchemy.** Workable, but the backend will be I/O-heavy (GitHub API, LLM calls,
  DB), and FastAPI plus asyncpg keeps one concurrency model throughout.

## Tradeoffs

- RLS adds a per-transaction `set_config` call and makes every unbound query return nothing,
  which surprises developers until they learn to bind first. We accept this; it fails closed.
- RLS is inert for superusers and roles with `BYPASSRLS`. Supabase's default `postgres`
  role has `BYPASSRLS`, so production must connect as a dedicated application role.
- `TRUNCATE` and foreign-key checks are not subject to RLS; the composite FK covers the
  second, and the first is reserved for tests and maintenance.
- A single deployable means one bad module can take the whole process down; module
  boundaries and tests are the mitigation until measurements justify extraction.

## Consequences

- Every new org-scoped table must: carry a non-null `org_id`, enable and force RLS with the
  `tenant_isolation` policy in its migration, and be covered by an isolation test.
  `tests/test_migrations.py` checks RLS is enabled and forced on the listed tenant tables.
- All org-scoped work must run inside a transaction bound with `require_membership` (or
  `create_organization`).
- Background workers (ingestion, agent runs) must bind to the job's org the same way.

## Open decisions

- **Identity provider** (Supabase Auth vs. another OIDC provider) and how its subject maps
  to `users`. Not decided; `users` currently has only email and display name.
- **Cross-org lookups for a user** ("which orgs am I in?") are blocked by RLS on
  `memberships`. Options: a second policy keyed on an `app.current_user_id` setting, or a
  narrow `SECURITY DEFINER` function. To be decided with the auth work.
- **Production database roles:** exact role names and grants on Supabase (migration role
  vs. runtime role without `BYPASSRLS`).
- **Repository uniqueness:** currently unique per `(org_id, github_repo_id)`. Whether one
  GitHub repository may be connected to more than one org is a product decision.
