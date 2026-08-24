#!/usr/bin/env bash
# Deploy the Evidence Explorer to proxmox (demo.redevops.io/evidence), :8101.
# Standalone — does NOT touch the control-plane. Stages the real v0.3.0 kernel + contracts (incl. the
# runtime_contracts.store EvidenceStore seam), persists a real benchmark run, serves a DuckDB SQL UI.
set -euo pipefail
HOST="${EV_HOST:-192.168.40.105}"
HOST_DIR="${EV_HOST_DIR:-/projects/agentic-os/evidence-demo}"
SRC="$(cd "$(dirname "$0")" && pwd)"                         # .../agentic-os/deploy/evidence-explorer-demo
AGENTIC="$(cd "$SRC/../.." && pwd)"                          # agentic-os repo root (has agentic_os)
RC="${RUNTIME_CONTRACTS:-/mnt/backup/projects/runtime-contracts}"

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
echo "== stage build context (kernel + contracts incl. store seam) =="
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$AGENTIC/agentic_os" "$STAGE/"
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$RC/runtime_contracts" "$STAGE/"
cp "$SRC/app.py" "$SRC/Dockerfile" "$SRC/evidence-demo.compose.yml" "$STAGE/"

echo "== sync -> $HOST:$HOST_DIR =="
ssh "root@$HOST" "mkdir -p $HOST_DIR"
rsync -aH --delete --exclude '.git' "$STAGE"/ "root@$HOST:$HOST_DIR/"

ssh "root@$HOST" EV_HOST_DIR="$HOST_DIR" 'bash -s' <<'REMOTE'
set -e
cd "$EV_HOST_DIR"
EVIDENCE_EDGE_PORT=8101 docker compose -p evidence-demo -f evidence-demo.compose.yml up -d --build
for i in $(seq 1 30); do
  curl -s --max-time 6 http://127.0.0.1:8101/healthz 2>/dev/null | grep -q '"ok":true' && { echo healthy; break; }
  sleep 2
done
curl -s http://127.0.0.1:8101/api/evidence/summary 2>/dev/null | head -c 220; echo
REMOTE

echo
echo "== ingress: add a demo.redevops.io path rule ^/(evidence|api/evidence) -> 192.168.40.105:8101"
