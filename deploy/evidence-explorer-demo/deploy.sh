#!/usr/bin/env bash
# Deploy the Evidence Explorer to the demo host (demo.redevops.io/evidence), :8101.
# Standalone — does NOT touch the control-plane. Stages the real v0.3.0 kernel + contracts (incl. the
# runtime_contracts.store EvidenceStore seam), persists a real benchmark run, serves a DuckDB SQL UI.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/../lib/deploy-env.sh"; deploy_load_env
deploy_require EV_HOST "the SSH address of the target host"
HOST="$EV_HOST"; HOST_DIR="${EV_HOST_DIR:-/opt/agentic-os/evidence-demo}"
SRC="$(cd "$(dirname "$0")" && pwd)"                         # .../agentic-os/deploy/evidence-explorer-demo
AGENTIC="$(cd "$SRC/../.." && pwd)"                          # agentic-os repo root (has agentic_os)
deploy_require RUNTIME_CONTRACTS "a checkout of the runtime-contracts package"
RC="$RUNTIME_CONTRACTS"

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
echo "== ingress: add a demo.redevops.io path rule ^/(evidence|api/evidence) -> $HOST:8101"
