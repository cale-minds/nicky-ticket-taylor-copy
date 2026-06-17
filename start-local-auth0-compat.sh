#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$ROOT/logs"

FASTAPI_HOST="${FASTAPI_HOST:-127.0.0.1}"
FASTAPI_PORT="${FASTAPI_PORT:-4200}"
APP_BASE_URL="${APP_BASE_URL:-http://localhost:$FASTAPI_PORT}"
AUTH0_CALLBACK_PATH="${AUTH0_CALLBACK_PATH:-/overview}"
AUTH0_DOMAIN="${AUTH0_DOMAIN:-dev-eq0ptfwdhb1s1h12.us.auth0.com}"
AUTH0_CLIENT_ID="${AUTH0_CLIENT_ID:-SqrJq2fxJ6adrOFaR24oh9COF4vZwqba}"
AUTH0_AUDIENCE="${AUTH0_AUDIENCE:-https://nicky-tech.azurewebsites.net}"
AUTH0_CLIENT_SECRET="${AUTH0_CLIENT_SECRET:-}"
ADMIN_ALLOWED_ROLES="${ADMIN_ALLOWED_ROLES:-Admin}"
NICKY_API_BASE_URL="${NICKY_API_BASE_URL:-https://api-public.dev.pay.nicky.me}"

if [[ -z "${FASTAPI_EXE:-}" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    FASTAPI_EXE="$ROOT/.venv/bin/python"
  else
    FASTAPI_EXE="python"
  fi
fi

export APP_BASE_URL
export AUTH0_CALLBACK_PATH
export AUTH0_DOMAIN
export AUTH0_CLIENT_ID
export AUTH0_AUDIENCE
export AUTH0_CLIENT_SECRET
export ADMIN_ALLOWED_ROLES
export NICKY_API_BASE_URL

cat <<EOF
Starting Nicky Ticket Tailor service in Auth0 local compatibility mode.

Admin UI:
$APP_BASE_URL/overview

Auth0 callback URL:
$APP_BASE_URL$AUTH0_CALLBACK_PATH

Allowed roles:
$ADMIN_ALLOWED_ROLES

Nicky API:
$NICKY_API_BASE_URL

Health:
$APP_BASE_URL/health

Keep this terminal open while testing.

EOF

cd "$ROOT"
exec "$FASTAPI_EXE" -m uvicorn app.main:app --host "$FASTAPI_HOST" --port "$FASTAPI_PORT"
