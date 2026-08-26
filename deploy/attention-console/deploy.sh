#!/usr/bin/env bash
# Deploy the Founder Attention Queue (the Business-OS home) to proxmox (demo.redevops.io/attention), :8104.
set -euo pipefail
HOST="${AT_HOST:-192.168.40.105}"; HOST_DIR="${AT_HOST_DIR:-/projects/agentic-os/attention-console}"
SRC="$(cd "$(dirname "$0")" && pwd)"; AGENTIC="$(cd "$SRC/../.." && pwd)"
RC="${RUNTIME_CONTRACTS:-/mnt/backup/projects/runtime-contracts}"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$AGENTIC/agentic_os" "$STAGE/"
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$RC/runtime_contracts" "$STAGE/"
cp "$SRC/app.py" "$SRC/Dockerfile" "$SRC/attention.compose.yml" "$STAGE/"
ssh "root@$HOST" "mkdir -p $HOST_DIR"
rsync -aH --delete --exclude '.git' "$STAGE"/ "root@$HOST:$HOST_DIR/"
ssh "root@$HOST" AT_HOST_DIR="$HOST_DIR" 'bash -s' <<'REMOTE'
set -e; cd "$AT_HOST_DIR"
ATTENTION_PORT=8104 docker compose -p attention-console -f attention.compose.yml up -d --build
for i in $(seq 1 30); do curl -s --max-time 6 http://127.0.0.1:8104/healthz 2>/dev/null | grep -q '"ok":true' && { echo healthy; break; }; sleep 2; done
curl -s "http://127.0.0.1:8104/api/attention" 2>/dev/null | head -c 200; echo
REMOTE
echo; echo "== ingress: add demo.redevops.io path rule ^/(attention|api/attention) -> 192.168.40.105:8104"
