#!/usr/bin/env bash
# Deploy the Business-OS Admin & Governance console to the demo host (demo.redevops.io/admin), :8105.
# LAGO_API_URL and LAGO_API_KEY are required and have no default: put them in a host-only .env in $AD_HOST_DIR on the
# target (never committed, kept across deploys) or export them there; `docker compose up` refuses to run without them.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/../lib/deploy-env.sh"; deploy_load_env
deploy_require AD_HOST "the SSH address of the target host"
HOST="$AD_HOST"; HOST_DIR="${AD_HOST_DIR:-/opt/agentic-os/admin-console}"
SRC="$(cd "$(dirname "$0")" && pwd)"; AGENTIC="$(cd "$SRC/../.." && pwd)"
deploy_require RUNTIME_CONTRACTS "a checkout of the runtime-contracts package"
RC="$RUNTIME_CONTRACTS"
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
echo; echo "== ingress: add demo.redevops.io path rule ^/(admin|api/admin) -> $HOST:8105"
