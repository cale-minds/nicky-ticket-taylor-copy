# Module layout

| Module | Responsibility |
|---|---|
| `app/main.py` | FastAPI entrypoint, route registration, middleware, lifespan |
| `app/config.py` | `Settings` (Pydantic), `get_settings()` cached |
| `app/db.py` | `Database` class, SQLAlchemy Core queries, date helpers |
| `app/db_models.py` | SQLAlchemy table definitions (`tenants`, `integration_orders`, `webhook_events`, `order_logs`) |
| `app/admin_ui.py` | FastAPI router for `/admin-ui/*`, server-rendered HTML via Python f-strings |
| `app/admin_auth.py` | Auth0 JWT decode, Admin/Support role helpers, `AdminUser` dataclass |
| `app/i18n.py` | `t(key)`, `set_request_locale()`, per-request `ContextVar`, JSON loading via `lru_cache` |
| `app/service.py` | `IntegrationService` — orchestrates business flows (create Nicky PR, confirm TT payment) |
| `app/tenants.py` | `TenantConfig` dataclass, normalization helpers |
| `app/nicky.py` | `NickyClient` — HTTP client for the Nicky API |
| `app/ticket_tailor.py` | `TicketTailorClient` — HTTP client for the Ticket Tailor API |
| `app/security.py` | HMAC signature verification for Ticket Tailor webhooks |
| `app/extractors.py` | Data extraction from Ticket Tailor raw payloads |
| `app/jobs.py` | Periodic job logic (expire orders, etc.) |
| `app/job_runner.py` | Job trigger endpoint and Bearer token authorization |
| `migrations/` | Alembic scripts — never modify tables without creating a migration |
| `app/translations/` | i18n JSON files (`en.json`, `pt-br.json`, etc.) — restart server after editing |
