#!/usr/bin/env bash
# Deploy the reference billing core (Lago-contract) to proxmox :8106 — proves the World Adapter LIVE path.
# REFCORE_API_KEY is required and has no default: put `REFCORE_API_KEY=<a secret of your own choosing>` in a host-only
# .env in $RC_HOST_DIR on the target (it is never committed and is kept across deploys), or export it on the target.
# `docker compose up` refuses to run, and the service refuses to start, without it.
set -euo pipefail
HOST="${RC_HOST:-192.168.40.105}"; HOST_DIR="${RC_HOST_DIR:-/projects/agentic-os/refcore-billing}"
SRC="$(cd "$(dirname "$0")" && pwd)"
ssh "root@$HOST" "mkdir -p $HOST_DIR"
rsync -aH --delete --exclude '.git' --exclude '.env' --exclude __pycache__ "$SRC"/ "root@$HOST:$HOST_DIR/"
ssh "root@$HOST" RC_HOST_DIR="$HOST_DIR" 'bash -s' <<'REMOTE'
set -e; cd "$RC_HOST_DIR"
REFCORE_PORT=8106 docker compose -p refcore-billing -f refcore.compose.yml up -d --build
for i in $(seq 1 20); do curl -s --max-time 5 http://127.0.0.1:8106/healthz 2>/dev/null | grep -q '"ok":true' && { echo healthy; break; }; sleep 2; done
REMOTE
