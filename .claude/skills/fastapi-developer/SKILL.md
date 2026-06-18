---
name: fastapi-developer
description: Generates Python/FastAPI backend code and architectural guidance for this service. Trigger when working on webhook routes, Nicky API or Ticket Tailor integration, the database layer (SQLAlchemy/Alembic), scheduled jobs, Auth0 JWT authentication, Vercel deployment, or any module under app/*.
license: MIT
metadata:
  author: Nicky
  version: '1.1'
---

# FastAPI Developer Guidelines (nicky-ticket-tailor-service)

1. **Python and dependencies**: the project requires Python ≥ 3.11 (`pyproject.toml`). All modules use `from __future__ import annotations`. When adding or removing a runtime dependency, update **both** `pyproject.toml` and `requirements.txt` — Vercel installs from `requirements.txt`.

2. **Database**: use SQLAlchemy Core directly via `Database` (`app/db.py`) — no ORM/Session, no repository pattern. Tables are defined in `app/db_models.py`. Schema changes go through Alembic migrations. Migrations are auto-applied at startup via `db.init()` — no need to run `alembic upgrade head` manually.

3. **Database access in routes**: access via `request.app.state.db`. Use `db.connect()` as a context manager for multi-query reads; `db._begin()` is internal to the `Database` class.

4. **Configuration**: all settings come from `app/config.py` via `get_settings()` (cached). Never read `os.environ` directly in routes or services.

5. **Authentication**: the FastAPI dependencies are `require_admin` (defined in `app/main.py`) for API routes and `require_admin_web` (defined in `app/admin_ui.py`) for the Admin UI. `app/admin_auth.py` provides only helpers: `is_admin`, `is_support`, `authenticate_admin_request`, `decode_and_verify_jwt`. Role-tier gating is done by calling `require_admin_role(user)` or `require_writer(user)` imperatively in the route body. **`admin_auth.require_admin` and `admin_auth.require_support` do not exist.**

6. **HTTP errors**: raise `HTTPException(status_code=..., detail=...)`. Never return error details inside 2xx response bodies.

7. **Async jobs**: periodic jobs live in `app/jobs.py` and are triggered via `app/job_runner.py` (authenticated by a Bearer token, not JWT/Auth0). On Vercel, the job runs via Cron (`vercel.json`); on a persistent host, via `RUN_BACKGROUND_JOBS=true`.

8. **After changes**: run `python -m pytest`. Tooling reality: there is no ruff, mypy, black, isort, Makefile, or CI in `.github/workflows/` (directory exists but is empty — ignore `.ruff_cache/.mypy_cache` entries in `.gitignore`). The only automated quality gate is pytest.

## Module layout

Where each responsibility lives: [modules.md](references/modules.md)

## Database

SQLAlchemy Core access conventions: [database.md](references/database.md)

## Migrations (Alembic)

Auto-apply on startup, how to create new ones: [migrations.md](references/migrations.md)

## Authentication and authorization

Auth0, FastAPI dependencies, job auth: [auth.md](references/auth.md)

## Nicky and Ticket Tailor integration

HTTP clients and API contracts: [integrations.md](references/integrations.md)

## Testing

Test suite patterns and conventions: [testing.md](references/testing.md)

## Deployment and local environment

Vercel, local scripts, requirements.txt: [deployment.md](references/deployment.md)
