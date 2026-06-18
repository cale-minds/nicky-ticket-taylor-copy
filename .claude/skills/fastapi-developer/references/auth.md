# Authentication and authorization

Two distinct mechanisms — do not mix them.

## 1. Auth0 — Admin UI and protected routes

Flow: Authorization Code + PKCE (S256). Accepts dual credential: signed Starlette session cookie (`admin_user`) **or** Bearer JWT verified via PyJWKClient (RS256).

**FastAPI dependencies (not in `admin_auth.py`):**

| Dependency | Defined in | Failure behavior |
|---|---|---|
| `require_admin` | `app/main.py:163` | Returns 401 JSON |
| `require_admin_web` | `app/admin_ui.py:84` | Redirects to `/admin-ui/login` |

**Role-tier gating (called imperatively in the route body):**

```python
# After the Depends yields an AdminUser, call in the route body:
require_admin_role(user)   # requires Admin role (main.py:178)
require_writer(user)        # accepts Admin or Support (main.py:196)
```

`require_admin_role` and `require_writer` are **plain helpers** — they take an `AdminUser`, not a `Request`, so they cannot be used as `Depends`.

**Helpers in `app/admin_auth.py`** (not FastAPI dependencies):
- `is_admin(user, settings)` — checks Admin role
- `is_support(user)` — checks Support role
- `authenticate_admin_request(request, settings)` → `AdminUser | None`
- `decode_and_verify_jwt(token, settings)` — decodes RS256
- `build_auth0_authorize_url(...)` — generates Auth0 login URL
- `exchange_auth0_code(...)` — exchanges code for token

**`admin_auth.require_admin` and `admin_auth.require_support` do not exist** — do not use those names.

## 2. Jobs — simple Bearer token (not JWT/Auth0)

Periodic job routes use `require_job_runner` (defined in `main.py:183`), which delegates to `is_authorized_job_request` in `app/job_runner.py`. Validation compares the Bearer token against `JOB_RUNNER_TOKEN` or `CRON_SECRET` using `secrets.compare_digest` (timing-attack resistant).

```python
@app.post("/jobs/expire-overdue-orders")
async def expire_orders(authorized: bool = Depends(require_job_runner)):
    ...
```

## Local configuration

Environment variables needed for Auth0 (see `launch.json` and `start-local-auth0-compat.bat`):
- `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_AUDIENCE`
- `ADMIN_ALLOWED_ROLES` (e.g. `"Admin"`)
- `APP_BASE_URL` (must match the callback URL registered in Auth0)
