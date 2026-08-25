#!/usr/bin/env bash
# Deploy the animated execution canvas to proxmox (demo.redevops.io/worlds), :8102. Standalone.
set -euo pipefail
HOST="${WC_HOST:-192.168.40.105}"; HOST_DIR="${WC_HOST_DIR:-/projects/agentic-os/world-canvas-demo}"
SRC="$(cd "$(dirname "$0")" && pwd)"; AGENTIC="$(cd "$SRC/../.." && pwd)"
RC="${RUNTIME_CONTRACTS:-/mnt/backup/projects/runtime-contracts}"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$AGENTIC/agentic_os" "$STAGE/"
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$RC/runtime_contracts" "$STAGE/"
cp "$SRC/app.py" "$SRC/Dockerfile" "$SRC/world-canvas.compose.yml" "$STAGE/"
ssh "root@$HOST" "mkdir -p $HOST_DIR"
rsync -aH --delete --exclude '.git' "$STAGE"/ "root@$HOST:$HOST_DIR/"
ssh "root@$HOST" WC_HOST_DIR="$HOST_DIR" 'bash -s' <<'REMOTE'
set -e; cd "$WC_HOST_DIR"
WORLD_CANVAS_PORT=8102 docker compose -p world-canvas -f world-canvas.compose.yml up -d --build
for i in $(seq 1 30); do curl -s --max-time 6 http://127.0.0.1:8102/healthz 2>/dev/null | grep -q '"ok":true' && { echo healthy; break; }; sleep 2; done
curl -s "http://127.0.0.1:8102/api/worlds/run?world=after-hours-lead" 2>/dev/null | head -c 160; echo
REMOTE
echo; echo "== ingress: add demo.redevops.io path rule ^/(worlds|api/worlds) -> 192.168.40.105:8102"
