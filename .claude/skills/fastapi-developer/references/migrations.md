# Migrations (Alembic)

## Migrations are applied automatically

**No need to run `alembic upgrade head` manually to start the app or run tests.** The chain is:

```
lifespan (app/main.py:84-85)
  → db.init()
    → run_migrations(url)           # app/db.py:121-150
      → alembic command.upgrade("head")
```

Integration tests (`test_service.py`, `test_admin_ui.py`) call `db.init()` explicitly and also apply migrations inline. Manual `alembic upgrade head` is only needed when running the Alembic CLI standalone (e.g. to inspect state or perform a downgrade).

## Configuration

`alembic.ini` (project root):
- `script_location = migrations`
- `prepend_sys_path = .`

`migrations/env.py` injects the metadata and the URL:
```python
from app.db_models import metadata
target_metadata = metadata
# URL injected at runtime via config.attributes["database_url"]
# (does NOT read sqlalchemy.url from the .ini)
```

## Creating a new migration

```bash
alembic revision --autogenerate -m "describe_the_change"
```

**Always review the generated file** under `migrations/versions/` before committing — `autogenerate` can mishandle columns with `server_default` or custom types.

## Dialects

- **Dev**: SQLite (file at `data/dev.db` by default)
- **Prod**: SQL Server

Test migrations against both dialects before deploying. No other dialect is officially supported.

## Current state

Baseline in `migrations/versions/0001_initial_schema.py` — the only current head.
