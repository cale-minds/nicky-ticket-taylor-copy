# Testing

## How to run

```bash
python -m pytest
```

Config in `pyproject.toml` (`[tool.pytest.ini_options]`): `testpaths = ["tests"]`, `asyncio_mode = "auto"` (async tests work without `@pytest.mark.asyncio`, though existing tests still apply it — both are valid).

## Two test styles

### 1. Pure unit tests (no DB, no HTTP clients)

For modules like `app/security.py`, `app/extractors.py`, `app/job_runner.py`: just import and call functions. No DB fixtures, no special setup. See `test_security.py`, `test_jobs.py`, `test_extractors.py`.

### 2. Integration / HTTP tests (need DB and clients)

For `app/service.py` and `app/admin_ui.py`. See `test_service.py` and `test_admin_ui.py`.

## Rules for integration tests

**Real DB — never mock.** Use a SQLite file under pytest's `tmp_path` fixture:

```python
settings = Settings(database_path=tmp_path / "integration.sqlite3")
db = Database(settings.database_path)
db.init()  # applies all migrations automatically
```

**External HTTP (Nicky / Ticket Tailor) — fake by subclassing.** Never install or use `respx`, `responses`, `unittest.mock`, or `pytest-mock` — none are installed. Dev deps are only `pytest` + `pytest-asyncio`. Instead, subclass the real client and override only the method under test:

```python
class FakeNickyClient(NickyClient):
    async def create_payment_request(self, *, api_key, ...):
        return {"payment_request_id": "fake-pr-id", ...}
```

See `test_service.py` (FakeNickyClient, FakeTicketTailorClient, SelectiveFailureTicketTailorClient) and `test_admin_ui.py` (FakeAdminNickyClient, FakeCommonUserNickyClient, etc.).

## Admin UI HTTP tests

Use the `build_test_client(tmp_path, nicky_client_class=..., **settings_overrides) -> TestClient` helper from `test_admin_ui.py` — it mounts the router, injects the DB, adds SessionMiddleware, and accepts settings overrides.

To simulate authentication without real Auth0, use the cookie helpers:
- `sign_admin_session(client, user)` — signs a Starlette session cookie
- `authenticate_admin(client, tmp_path)` — Admin role user
- `authenticate_support(client, tmp_path)` — Support role user
- `authenticate_common_user(client, tmp_path)` — user with no admin role

## No conftest.py

There is no global `conftest.py`. Each test file defines its own helpers inline (e.g. `live_tenant`, `seed_tenant`, `seed_expired_order`). Do not create a `conftest.py` — follow the existing pattern.
