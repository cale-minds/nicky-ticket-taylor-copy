#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OUT_LOG="${OUT_LOG:-"$ROOT/logs/uvicorn-real-nicky.out.log"}"
ERR_LOG="${ERR_LOG:-"$ROOT/logs/uvicorn-real-nicky.err.log"}"
TUNNEL_LOG="${TUNNEL_LOG:-"$ROOT/logs/cloudflared-sh.log"}"

echo "Following microservice logs..."
echo
echo "STDOUT/access log:"
echo "$OUT_LOG"
echo
echo "STDERR/stack traces:"
echo "$ERR_LOG"
echo
echo "Tunnel log:"
echo "$TUNNEL_LOG"
echo

mkdir -p "$ROOT/logs"
touch "$OUT_LOG" "$ERR_LOG" "$TUNNEL_LOG"

tail -n 120 -F "$OUT_LOG" "$ERR_LOG" "$TUNNEL_LOG"
