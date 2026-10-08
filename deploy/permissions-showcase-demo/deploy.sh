#!/usr/bin/env bash
# Deploy the data-access permissions plane showcase to proxmox (demo.redevops.io/permissions), :8109.
# Standalone + v6-native — does NOT resurrect the retired control-plane. Stages the real kernel
# permissions module (agentic_os.permissions + views) only.
set -euo pipefail
HOST="${PERM_HOST:-192.168.40.105}"
HOST_DIR="${PERM_HOST_DIR:-/projects/agentic-os/permissions-demo}"
SRC="$(cd "$(dirname "$0")" && pwd)"                         # .../agentic-os/deploy/permissions-showcase-demo
AGENTIC="$(cd "$SRC/../.." && pwd)"                          # agentic-os repo root (has agentic_os)

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
echo "== stage build context (kernel permissions plane) =="
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$AGENTIC/agentic_os" "$STAGE/"
cp "$SRC/app.py" "$SRC/Dockerfile" "$SRC/permissions-demo.compose.yml" "$STAGE/"

echo "== sync -> $HOST:$HOST_DIR =="
ssh "root@$HOST" "mkdir -p $HOST_DIR"
rsync -aH --delete --exclude '.git' "$STAGE"/ "root@$HOST:$HOST_DIR/"

ssh "root@$HOST" PERM_HOST_DIR="$HOST_DIR" 'bash -s' <<'REMOTE'
set -e
cd "$PERM_HOST_DIR"
PERM_EDGE_PORT=8109 docker compose -p permissions-demo -f permissions-demo.compose.yml up -d --build
for i in $(seq 1 30); do
  curl -s --max-time 6 http://127.0.0.1:8109/healthz 2>/dev/null | grep -q '"ok":true' && { echo healthy; break; }
  sleep 2
done
echo -n "status: "; curl -s http://127.0.0.1:8109/api/permissions/status 2>/dev/null | head -c 200; echo
echo -n "page:   "; curl -s -o /dev/null -w "/permissions -> %{http_code}\n" http://127.0.0.1:8109/permissions
REMOTE

echo
echo "== ingress: add demo.redevops.io path rule ^/(permissions|api/permissions) -> 192.168.40.105:8109"
echo "   (edit /main/cloudflared/config.yml BEFORE the bare demo.redevops.io catch-all, then restart cloudflared)"
