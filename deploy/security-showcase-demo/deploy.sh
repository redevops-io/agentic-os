#!/usr/bin/env bash
# Deploy the intrinsic-security + telemetry showcase to proxmox (demo.redevops.io/security), :8099.
# Standalone — does NOT touch the live control-plane. Stages the real v0.3.0 kernel + contracts.
set -euo pipefail
HOST="${SEC_HOST:-192.168.40.105}"
HOST_DIR="${SEC_HOST_DIR:-/projects/agentic-os/security-demo}"
SRC="$(cd "$(dirname "$0")" && pwd)"                         # .../agentic-os/deploy/security-showcase-demo
AGENTIC="$(cd "$SRC/../.." && pwd)"                          # agentic-os repo root (has agentic_os)
RC="${RUNTIME_CONTRACTS:-/mnt/backup/projects/runtime-contracts}"

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
echo "== stage build context (kernel + contracts) =="
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$AGENTIC/agentic_os" "$STAGE/"
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$RC/runtime_contracts" "$STAGE/"
cp "$SRC/app.py" "$SRC/Dockerfile" "$SRC/security-demo.compose.yml" "$STAGE/"

echo "== sync -> $HOST:$HOST_DIR =="
ssh "root@$HOST" "mkdir -p $HOST_DIR"
rsync -aH --delete --exclude '.git' "$STAGE"/ "root@$HOST:$HOST_DIR/"

ssh "root@$HOST" SEC_HOST_DIR="$HOST_DIR" 'bash -s' <<'REMOTE'
set -e
cd "$SEC_HOST_DIR"
SEC_EDGE_PORT=8099 docker compose -p security-demo -f security-demo.compose.yml up -d --build
for i in $(seq 1 30); do
  curl -s --max-time 6 http://127.0.0.1:8099/healthz 2>/dev/null | grep -q '"ok":true' && { echo healthy; break; }
  sleep 2
done
curl -s http://127.0.0.1:8099/api/security/scenario 2>/dev/null | head -c 160; echo
REMOTE

echo
echo "== ingress: add a demo.redevops.io path rule ^/(security|api/security) -> 192.168.40.105:8099"
