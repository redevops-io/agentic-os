#!/usr/bin/env bash
# Deploy the data-access permissions plane showcase to proxmox (demo.redevops.io/permissions), :8109.
# Standalone + v6-native — does NOT resurrect the retired control-plane. Stages the real kernel
# permissions module (agentic_os.permissions + views) only. DO NOT run it from a script you have not read.
#
# THE KEY. The grant store needs PERMISSIONS_KEY (and the admin API needs PERMISSIONS_ADMIN_KEY). They live ONLY on the host, in
#   $HOST_DIR/permissions.env   (mode 0600, created on the host, never copied from here, never printed)
# and compose reads them with `--env-file permissions.env`. This script never creates, overwrites or deletes that file: the sync below
# EXCLUDES key and env files from --delete. First time on a host:   deploy.sh --init-key   (generates the key ON the host, 0600, once).
# A host whose volume already holds a .permissions.key keeps its key: copy the same key into permissions.env on the host, do not generate a new one.
# The container runs as uid 10001 (read-only root fs, no capabilities): a volume created by an earlier root image needs ONE `chown -R 10001:10001` (see the Dockerfile).
# The app binds ONE address (PERM_BIND_HOST, default: the host address cloudflared reaches), never 0.0.0.0.
set -euo pipefail
HOST="${PERM_HOST:-192.168.40.105}"
HOST_DIR="${PERM_HOST_DIR:-/projects/agentic-os/permissions-demo}"
SRC="$(cd "$(dirname "$0")" && pwd)"                         # .../agentic-os/deploy/permissions-showcase-demo
AGENTIC="$(cd "$SRC/../.." && pwd)"                          # agentic-os repo root (has agentic_os)

if [ "${1:-}" = "--init-key" ]; then
  echo "== generate the grant-store key ON $HOST (once; never printed, never transferred) =="
  ssh "root@$HOST" "mkdir -p $HOST_DIR && cd $HOST_DIR && umask 077 && [ -e permissions.env ] || python3 -c 'import base64,os;print(\"PERMISSIONS_KEY=\"+base64.b64encode(os.urandom(32)).decode())' > permissions.env; chmod 600 permissions.env; test -s permissions.env && echo 'permissions.env present (0600)'"
  echo "   now add PERMISSIONS_ADMIN_KEY=<your admin key> (24+ characters) AND PERMISSIONS_TRUSTED_PROXIES=<the proxy hop address> to $HOST_DIR/permissions.env on the host (the admin API is disabled without the first; deploy.sh refuses without the second)."
  exit 0
fi

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
echo "== stage build context (kernel permissions plane) =="
rsync -aH --exclude __pycache__ --exclude '*.pyc' "$AGENTIC/agentic_os" "$STAGE/"
cp "$SRC/app.py" "$SRC/Dockerfile" "$SRC/permissions-demo.compose.yml" "$STAGE/"

echo "== sync -> $HOST:$HOST_DIR =="
ssh "root@$HOST" "mkdir -p $HOST_DIR"
# --delete must never remove what this script did not create: key and env files stay on the host
rsync -aH --delete --exclude '.git' --exclude 'permissions.env' --exclude '.permissions.key' --exclude '.env' --exclude '*.key' "$STAGE"/ "root@$HOST:$HOST_DIR/"

ssh "root@$HOST" PERM_HOST_DIR="$HOST_DIR" PERM_BIND="${PERM_BIND_HOST:-$HOST}" 'bash -s' <<'REMOTE'
set -e
cd "$PERM_HOST_DIR"
if ! grep -q '^PERMISSIONS_KEY=.' permissions.env 2>/dev/null; then
  echo "error: the grant-store key is not provisioned on the host ($PERM_HOST_DIR/permissions.env has no PERMISSIONS_KEY)." >&2
  echo "       run: deploy.sh --init-key   (or put the volume's existing key into permissions.env on the host)" >&2
  exit 1
fi
grep -q '^PERMISSIONS_ADMIN_KEY=.' permissions.env || echo "warning: no PERMISSIONS_ADMIN_KEY in permissions.env: the admin API will be disabled (503)" >&2
if ! grep -q '^PERMISSIONS_TRUSTED_PROXIES=.' permissions.env 2>/dev/null; then
  echo "error: PERMISSIONS_TRUSTED_PROXIES is required and is not set in $PERM_HOST_DIR/permissions.env." >&2
  echo "       It is the address of the proxy hop in front of this service (the cloudflared host, e.g. 127.0.0.1). Without it every visitor shares ONE" >&2
  echo "       failed-attempt bucket and the admin API cannot tell them apart. Add PERMISSIONS_TRUSTED_PROXIES=<address> to permissions.env on the host, then re-run." >&2
  exit 1
fi
PERM_EDGE_PORT=8109 PERM_BIND_HOST="$PERM_BIND" docker compose --env-file permissions.env -p permissions-demo -f permissions-demo.compose.yml up -d --build
for i in $(seq 1 30); do
  curl -s --max-time 6 "http://$PERM_BIND:8109/healthz" 2>/dev/null | grep -q '"ok":true' && { echo healthy; break; }
  sleep 2
done
echo -n "demo:   "; curl -s "http://$PERM_BIND:8109/api/demo/scenarios" 2>/dev/null | head -c 120; echo
echo -n "page:   "; curl -s -o /dev/null -w "/permissions -> %{http_code}\n" "http://$PERM_BIND:8109/permissions"
REMOTE

echo
echo "== ingress: add demo.redevops.io path rule ^/(permissions|api/demo|api/permissions) -> 192.168.40.105:8109 (api/permissions is the ADMIN API: it answers 401 without the admin key)"
echo "   (edit /main/cloudflared/config.yml BEFORE the bare demo.redevops.io catch-all, then restart cloudflared)"
