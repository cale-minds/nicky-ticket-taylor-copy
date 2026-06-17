#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TOOLS_DIR="${TOOLS_DIR:-"$ROOT/tools"}"
CLOUDFLARED="${CLOUDFLARED:-"$TOOLS_DIR/cloudflared"}"
LOG_DIR="${LOG_DIR:-"$ROOT/logs"}"
LOG_FILE="${LOG_FILE:-"$LOG_DIR/cloudflared-sh.log"}"
FASTAPI_LOG_FILE="${FASTAPI_LOG_FILE:-"$LOG_DIR/fastapi-sh.log"}"
URL_FILE="${URL_FILE:-"$ROOT/tunnel-urls.txt"}"

LOCAL_URL="${LOCAL_URL:-http://127.0.0.1:8017}"
FASTAPI_HOST="${FASTAPI_HOST:-127.0.0.1}"
FASTAPI_PORT="${FASTAPI_PORT:-8017}"
START_FASTAPI="${START_FASTAPI:-true}"
RESTART_FASTAPI="${RESTART_FASTAPI:-true}"
FASTAPI_EXE="${FASTAPI_EXE:-}"
TENANT_ID="${TENANT_ID:-}"

download() {
  local url="$1"
  local output="$2"

  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$output"
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$output"
  else
    echo "[ERROR] curl or wget is required to download cloudflared." >&2
    return 1
  fi
}

download_cloudflared() {
  local os
  local arch
  local url
  local tmp_dir

  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m | tr '[:upper:]' '[:lower:]')"

  case "$os:$arch" in
    linux:x86_64|linux:amd64)
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
      ;;
    linux:aarch64|linux:arm64)
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
      ;;
    darwin:x86_64|darwin:amd64)
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
      ;;
    darwin:arm64|darwin:aarch64)
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz"
      ;;
    *)
      echo "[ERROR] Unsupported platform for automatic cloudflared download: $os/$arch" >&2
      return 1
      ;;
  esac

  mkdir -p "$TOOLS_DIR"

  if [[ "$os" == "darwin" ]]; then
    tmp_dir="$(mktemp -d)"
    download "$url" "$tmp_dir/cloudflared.tgz"
    tar -xzf "$tmp_dir/cloudflared.tgz" -C "$tmp_dir"
    mv "$tmp_dir/cloudflared" "$CLOUDFLARED"
    rm -rf "$tmp_dir"
  else
    download "$url" "$CLOUDFLARED"
  fi

  chmod +x "$CLOUDFLARED"
}

if [[ ! -x "$CLOUDFLARED" ]]; then
  echo "cloudflared not found at:"
  echo "$CLOUDFLARED"
  echo
  echo "Downloading cloudflared into tools..."
  download_cloudflared
fi

if ! "$CLOUDFLARED" --version >/dev/null 2>&1; then
  echo "[ERROR] cloudflared exists but did not execute correctly:" >&2
  echo "$CLOUDFLARED" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
rm -f "$LOG_FILE" "$URL_FILE"

precheck_fastapi_runtime() {
  if [[ "$RESTART_FASTAPI" != "true" ]] && curl -fsS "$LOCAL_URL/health" >/dev/null 2>&1; then
    return 0
  fi

  if [[ -z "$FASTAPI_EXE" ]]; then
    if [[ -x "$ROOT/.venv/bin/python" ]]; then
      FASTAPI_EXE="$ROOT/.venv/bin/python"
    else
      FASTAPI_EXE="python"
    fi
  fi

  if "$FASTAPI_EXE" -c 'import uvicorn' >/dev/null 2>&1; then
    return 0
  fi

  cat >&2 <<EOF
[ERROR] The helper needs to start FastAPI, but the selected runtime does not provide the 'uvicorn' module.
Runtime checked:
$FASTAPI_EXE

Fix the environment before opening the tunnel:
  python3.11 -m venv .venv
  source .venv/bin/activate
  pip install -e .

Or start the service manually and rerun with:
  START_FASTAPI=false ./start-cloudflare-tunnel.sh
EOF
  return 1
}

precheck_auth0() {
  (cd "$ROOT" && "$FASTAPI_EXE" -c 'from app.config import get_settings; from app.admin_auth import auth0_enabled; raise SystemExit(0 if auth0_enabled(get_settings()) else 1)') >/dev/null 2>&1 && return 0

  cat >&2 <<EOF
[ERROR] Auth0 is required, but AUTH0_DOMAIN/AUTH0_CLIENT_ID are not configured.

Create .env from .env.example or export before running:
  export AUTH0_DOMAIN=your-tenant.auth0.com
  export AUTH0_CLIENT_ID=your-client-id
  export AUTH0_AUDIENCE=optional-audience
  export ADMIN_ALLOWED_ROLES=Admin

For local testing with the development Auth0 client, use:
  ./start-local-auth0-compat.sh
EOF
  return 1
}

stop_fastapi_on_port() {
  if [[ -f "$LOG_DIR/fastapi.pid" ]]; then
    local pid
    pid="$(cat "$LOG_DIR/fastapi.pid" 2>/dev/null || true)"
    if [[ -n "$pid" ]]; then
      kill "$pid" >/dev/null 2>&1 || true
    fi
  fi

  if command -v lsof >/dev/null 2>&1; then
    while read -r pid; do
      [[ -n "$pid" ]] || continue
      echo "Restarting FastAPI: stopping process on port $FASTAPI_PORT (PID $pid) ..."
      kill "$pid" >/dev/null 2>&1 || true
    done < <(lsof -tiTCP:"$FASTAPI_PORT" -sTCP:LISTEN 2>/dev/null || true)
  fi

  sleep 1
}

if [[ "$START_FASTAPI" == "true" ]]; then
  precheck_fastapi_runtime || exit 1
  precheck_auth0 || exit 1
fi

echo "Starting Cloudflare Tunnel for $LOCAL_URL ..."
echo "Log: $LOG_FILE"
echo

nohup "$CLOUDFLARED" tunnel --url "$LOCAL_URL" --no-autoupdate >"$LOG_FILE" 2>&1 &
TUNNEL_PID="$!"
echo "$TUNNEL_PID" > "$LOG_DIR/cloudflared.pid"

PUBLIC_URL=""
for _ in $(seq 1 60); do
  if [[ -f "$LOG_FILE" ]]; then
    PUBLIC_URL="$(grep -Eo 'https://[-a-zA-Z0-9.]+\.trycloudflare\.com' "$LOG_FILE" | tail -n 1 || true)"
  fi
  if [[ -n "$PUBLIC_URL" ]]; then
    break
  fi
  sleep 1
done

if [[ -z "$PUBLIC_URL" ]]; then
  echo "[ERROR] Could not capture the Cloudflare Tunnel URL." >&2
  echo "See log: $LOG_FILE" >&2
  echo "Tunnel PID: $TUNNEL_PID" >&2
  exit 1
fi

HEALTH_URL="$PUBLIC_URL/health"
DOCS_URL="$PUBLIC_URL/docs"
ADMIN_UI_URL="$PUBLIC_URL/admin-ui"
AUTH0_CALLBACK_URL="$PUBLIC_URL/admin-ui/callback"
if [[ -n "$TENANT_ID" ]]; then
  TICKET_TAILOR_WEBHOOK_URL="$PUBLIC_URL/webhooks/ticket-tailor/$TENANT_ID"
else
  TICKET_TAILOR_WEBHOOK_URL="$PUBLIC_URL/webhooks/ticket-tailor/{tenant_uuid}"
fi
NICKY_SUCCESS_URL="$PUBLIC_URL/nicky/success"
NICKY_CANCEL_URL="$PUBLIC_URL/nicky/cancel"

if [[ "$START_FASTAPI" == "true" ]]; then
  if [[ "$RESTART_FASTAPI" == "true" ]]; then
    stop_fastapi_on_port
  fi

  if curl -fsS "$LOCAL_URL/health" >/dev/null 2>&1; then
    echo "FastAPI already responded at $LOCAL_URL/health."
    echo "If Auth0 must use the public URL, confirm the current process was started with APP_BASE_URL=$PUBLIC_URL."
  else
    if [[ -z "$FASTAPI_EXE" ]]; then
      if [[ -x "$ROOT/.venv/bin/python" ]]; then
        FASTAPI_EXE="$ROOT/.venv/bin/python"
      else
        FASTAPI_EXE="python"
      fi
    fi

    echo "FastAPI did not respond at $LOCAL_URL/health."
    echo "Starting FastAPI with APP_BASE_URL=$PUBLIC_URL ..."
    APP_BASE_URL="$PUBLIC_URL" \
      NICKY_SUCCESS_URL="$NICKY_SUCCESS_URL" \
      NICKY_CANCEL_URL="$NICKY_CANCEL_URL" \
      nohup "$FASTAPI_EXE" -m uvicorn app.main:app --host "$FASTAPI_HOST" --port "$FASTAPI_PORT" >"$FASTAPI_LOG_FILE" 2>&1 &
    FASTAPI_PID="$!"
    echo "$FASTAPI_PID" > "$LOG_DIR/fastapi.pid"

    for _ in $(seq 1 30); do
      if curl -fsS "$LOCAL_URL/health" >/dev/null 2>&1; then
        echo "FastAPI ready at $LOCAL_URL"
        break
      fi
      sleep 1
    done

    if ! curl -fsS "$LOCAL_URL/health" >/dev/null 2>&1; then
      echo "[WARN] FastAPI was launched, but the health check still did not respond." >&2
      echo "Review logs:" >&2
      echo "$FASTAPI_LOG_FILE" >&2
      exit 1
    fi
  fi
fi

cat > "$URL_FILE" <<EOF
Public URL: $PUBLIC_URL
Health: $HEALTH_URL
Docs: $DOCS_URL
Admin UI: $ADMIN_UI_URL
Auth0 callback URL: $AUTH0_CALLBACK_URL

Ticket Tailor webhook:
$TICKET_TAILOR_WEBHOOK_URL

Nicky webhook:
Cadastrado automaticamente ao salvar o tenant na UI.

Nicky successUrl:
$NICKY_SUCCESS_URL

Nicky cancelUrl:
$NICKY_CANCEL_URL

Local service expected at:
$LOCAL_URL

FastAPI log:
$FASTAPI_LOG_FILE

Cloudflared log:
$LOG_FILE

Cloudflared PID:
$TUNNEL_PID
EOF

echo "Generated URLs:"
echo
cat "$URL_FILE"
echo
echo "Saved at:"
echo "$URL_FILE"
echo
echo "Tunnel is running in the background. Stop it with:"
echo "kill $TUNNEL_PID"
