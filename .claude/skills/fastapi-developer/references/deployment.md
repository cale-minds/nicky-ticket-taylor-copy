# Deployment and local environment

## Running locally

```bash
# Standard mode (port 8017, no Auth0 callback)
uvicorn app.main:app --reload --port 8017

# Auth0 mode (port 4200, callback configured in the Auth0 dev tenant)
start-local-auth0-compat.bat   # Windows
./start-local-auth0-compat.sh  # Linux/Mac
```

`app/main.py` has no `if __name__ == '__main__'` guard — always use `uvicorn` directly.

The `.claude/launch.json` mirrors these two configurations (port 4200 for Auth0, port 8017 for tunnel).

## Helper scripts

| Script | What it does |
|---|---|
| `start-cloudflare-tunnel.bat/.sh` | Downloads `tools/cloudflared` if missing, exposes port 8017, writes URL to `tunnel-urls.txt` |
| `start-local-auth0-compat.bat/.sh` | Starts the app on port 4200 with Auth0 dev env vars |
| `tail-microservice-logs.sh` | Tails `logs/uvicorn-real-nicky.*.log` + `cloudflared-sh.log` |

## Vercel (production)

Entry point: `api/index.py` — contains only `from app.main import app`.

`vercel.json` defines:
- Build: `@vercel/python` pointing to `api/index.py`
- Routes: `/`, `/admin-ui`, `/overview`, `/authentication/login-callback`, `/api/*` → `api/index.py`
- Cron: `0 3 * * *` UTC → `POST /api/jobs/expire-overdue-orders` (authenticated by `JOB_RUNNER_TOKEN`/`CRON_SECRET`)

### ⚠️ Keep requirements.txt in sync with pyproject.toml

Vercel installs from `requirements.txt` — it does **not** read `pyproject.toml`. When adding or removing a runtime dependency, update both files. An out-of-sync `requirements.txt` causes an `ImportError` at deploy time.

### RUN_BACKGROUND_JOBS

Must be `false` on Vercel (serverless — no persistent loop; expiry runs via Cron). Set to `true` only on persistent hosts. Defaults to `false` in `config.py`.

## Tooling

There is **no** ruff, mypy, black, isort, Makefile, Dockerfile, or CI in `.github/workflows/` (directory exists but is empty). Ignore the `.ruff_cache/` and `.mypy_cache/` entries in `.gitignore` — they are aspirational. The only automated quality gate is `python -m pytest`.
