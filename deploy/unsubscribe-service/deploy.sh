#!/usr/bin/env bash
# Deploy the unsubscribe endpoint to proxmox (demo.redevops.io/unsubscribe), :8103. Persistent ledger.
set -euo pipefail
HOST="${U_HOST:-192.168.40.105}"; HOST_DIR="${U_HOST_DIR:-/projects/agentic-os/unsubscribe-service}"
SRC="$(cd "$(dirname "$0")" && pwd)"; AGENTIC="$(cd "$SRC/../.." && pwd)"
RC="${RUNTIME_CONTRACTS:-/mnt/backup/projects/runtime-contracts}"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$AGENTIC/agentic_os" "$STAGE/"
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$RC/runtime_contracts" "$STAGE/"
cp "$SRC/app.py" "$SRC/Dockerfile" "$SRC/unsubscribe.compose.yml" "$STAGE/"
ssh "root@$HOST" "mkdir -p $HOST_DIR /projects/gtm/suppression"
rsync -aH --delete --exclude '.git' "$STAGE"/ "root@$HOST:$HOST_DIR/"
ssh "root@$HOST" U_HOST_DIR="$HOST_DIR" 'bash -s' <<'REMOTE'
set -e; cd "$U_HOST_DIR"
UNSUB_PORT=8103 docker compose -p unsubscribe -f unsubscribe.compose.yml up -d --build
for i in $(seq 1 30); do curl -s --max-time 6 http://127.0.0.1:8103/healthz 2>/dev/null | grep -q '"ok":true' && { echo healthy; break; }; sleep 2; done
REMOTE
echo; echo "== ingress: add demo.redevops.io path rule ^/(unsubscribe|api/unsubscribe) -> 192.168.40.105:8103"
