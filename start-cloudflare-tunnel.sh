#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TOOLS_DIR="${TOOLS_DIR:-"$ROOT/tools"}"
CLOUDFLARED="${CLOUDFLARED:-"$TOOLS_DIR/cloudflared"}"
LOG_DIR="${LOG_DIR:-"$ROOT/logs"}"
LOG_FILE="${LOG_FILE:-"$LOG_DIR/cloudflared-sh.log"}"
URL_FILE="${URL_FILE:-"$ROOT/tunnel-urls.txt"}"

LOCAL_URL="${LOCAL_URL:-http://127.0.0.1:8017}"
TENANT_ID="${TENANT_ID:-demo-tenant}"
NICKY_WEBHOOK_TOKEN="${NICKY_WEBHOOK_TOKEN:-tenant_webhook_token_here}"

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
TICKET_TAILOR_WEBHOOK_URL="$PUBLIC_URL/webhooks/ticket-tailor/$TENANT_ID"
NICKY_WEBHOOK_URL="$PUBLIC_URL/webhooks/nicky/$TENANT_ID?token=$NICKY_WEBHOOK_TOKEN"
NICKY_SUCCESS_URL="$PUBLIC_URL/nicky/success"
NICKY_CANCEL_URL="$PUBLIC_URL/nicky/cancel"

cat > "$URL_FILE" <<EOF
Public URL: $PUBLIC_URL
Health: $HEALTH_URL
Docs: $DOCS_URL

Ticket Tailor webhook:
$TICKET_TAILOR_WEBHOOK_URL

Nicky webhook:
$NICKY_WEBHOOK_URL

Nicky successUrl:
$NICKY_SUCCESS_URL

Nicky cancelUrl:
$NICKY_CANCEL_URL

Local service expected at:
$LOCAL_URL

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
