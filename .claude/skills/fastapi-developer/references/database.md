# Database and Alembic

## Data access

- Always use `Database` from `app/db.py`. Do not instantiate engines or sessions directly in routes.
- For composed reads (multiple `SELECT`s), use `db.connect()`:
  ```python
  with db.connect() as conn:
      row = conn.execute(select(tenants).where(...)).fetchone()
  ```
- Write operations use `db._begin()` internally inside the `Database` class — do not call `_begin()` outside of it.
- Always use `func.now()` for timestamps inside SQL queries; never `datetime.utcnow()` or `datetime.now()`.
- `utc_now()` (module-level function imported from `app/db.py`) is available for use outside SQL queries — returns a naive UTC datetime.

## Query conventions

- Prefer SQLAlchemy Core (`select()`, `.where()`, `.values()`) over raw SQL strings — except for very simple queries via `conn.execute(str, params)`.
- Positional `?` parameters are automatically rewritten to named params (`:p0`, `:p1`) by the `positional_sql` helper.
- `Record` is a `dict` subclass — treat it as a normal `dict`.

## Creating Alembic migrations

```bash
alembic revision --autogenerate -m "description"
# review the generated file under migrations/versions/
alembic upgrade head
```

- Always review the generated file — `autogenerate` can mishandle columns with `server_default`.
- Never use `DROP COLUMN` in production without checking for existing data.
- Dev database is SQLite (`data/dev.db`); production is SQL Server — test migrations against both.
