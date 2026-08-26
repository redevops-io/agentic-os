#!/usr/bin/env bash
# Deploy the Business-OS Admin & Governance console to proxmox (demo.redevops.io/admin), :8105.
set -euo pipefail
HOST="${AD_HOST:-192.168.40.105}"; HOST_DIR="${AD_HOST_DIR:-/projects/agentic-os/admin-console}"
SRC="$(cd "$(dirname "$0")" && pwd)"; AGENTIC="$(cd "$SRC/../.." && pwd)"
RC="${RUNTIME_CONTRACTS:-/mnt/backup/projects/runtime-contracts}"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$AGENTIC/agentic_os" "$STAGE/"
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$RC/runtime_contracts" "$STAGE/"
cp "$SRC/app.py" "$SRC/Dockerfile" "$SRC/admin.compose.yml" "$STAGE/"
ssh "root@$HOST" "mkdir -p $HOST_DIR"
rsync -aH --delete --exclude '.git' --exclude '.env' "$STAGE"/ "root@$HOST:$HOST_DIR/"
ssh "root@$HOST" AD_HOST_DIR="$HOST_DIR" 'bash -s' <<'REMOTE'
set -e; cd "$AD_HOST_DIR"
ADMIN_PORT=8105 docker compose -p admin-console -f admin.compose.yml up -d --build
for i in $(seq 1 30); do curl -s --max-time 6 http://127.0.0.1:8105/healthz 2>/dev/null | grep -q '"ok":true' && { echo healthy; break; }; sleep 2; done
curl -s "http://127.0.0.1:8105/api/admin" 2>/dev/null | head -c 200; echo
REMOTE
echo; echo "== ingress: add demo.redevops.io path rule ^/(admin|api/admin) -> 192.168.40.105:8105"
