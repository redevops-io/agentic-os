#!/usr/bin/env bash
# Deploy the unsubscribe endpoint to the demo host (demo.redevops.io/unsubscribe), :8103. Persistent ledger.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/../lib/deploy-env.sh"; deploy_load_env
deploy_require U_HOST "the SSH address of the target host"
HOST="$U_HOST"; HOST_DIR="${U_HOST_DIR:-/opt/agentic-os/unsubscribe-service}"
SRC="$(cd "$(dirname "$0")" && pwd)"; AGENTIC="$(cd "$SRC/../.." && pwd)"
deploy_require RUNTIME_CONTRACTS "a checkout of the runtime-contracts package"
RC="$RUNTIME_CONTRACTS"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$AGENTIC/agentic_os" "$STAGE/"
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$RC/runtime_contracts" "$STAGE/"
cp "$SRC/app.py" "$SRC/Dockerfile" "$SRC/unsubscribe.compose.yml" "$STAGE/"
DATA_DIR="${U_DATA_DIR:-$HOST_DIR/data}"      # where the opt-out ledger persists on the target
ssh "root@$HOST" "mkdir -p $HOST_DIR $DATA_DIR"
rsync -aH --delete --exclude '.git' "$STAGE"/ "root@$HOST:$HOST_DIR/"
ssh "root@$HOST" U_HOST_DIR="$HOST_DIR" UNSUB_DATA_DIR="$DATA_DIR" 'bash -s' <<'REMOTE'
set -e; cd "$U_HOST_DIR"
UNSUB_PORT=8103 docker compose -p unsubscribe -f unsubscribe.compose.yml up -d --build
for i in $(seq 1 30); do curl -s --max-time 6 http://127.0.0.1:8103/healthz 2>/dev/null | grep -q '"ok":true' && { echo healthy; break; }; sleep 2; done
REMOTE
echo; echo "== ingress: add demo.redevops.io path rule ^/(unsubscribe|api/unsubscribe) -> $HOST:8103"
