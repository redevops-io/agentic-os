#!/bin/bash
# Edge Sentinel UI audit: fixture core, loopback only. Server is configured so that an authenticated
# /execute is a REAL kick-off attempt (SENTINEL_BLOCK_ENABLED=1 + operator credentials): if fixture mode
# holds, core still refuses it. Credentials are random per run and live only in this process's env.
#
#   ES_PYTHON=<venv with the app's deps>  UI_PYTHON=<venv with playwright> \
#     scripts/run_ui_audit_edge_sentinel.sh [out-dir] [label]
#
# Both interpreters are parameters on purpose: the app's environment must not gain playwright, and the
# audit environment must not gain the app. DEMO_MODE=0 runs the same journeys without fixtures as a control.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ES_PYTHON=${ES_PYTHON:-python3}
UI_PYTHON=${UI_PYTHON:-python3}
PORT=8299
OUT=${1:-/tmp/ui-evidence}
LABEL=${2:-fixture}
CREDS="audit-operator:$(openssl rand -hex 12)"
mkdir -p "$OUT"
# DEMO_MODE=0 runs the same journeys against the un-fixture'd app, as a control: it separates a defect the
# fixture adapter introduced from one the dashboard already had. Without fixtures the app has no CrowdSec
# core to read, so its fixture-specific assertions are expected to fail in that mode.
DEMO_MODE=${DEMO_MODE:-1}

cd "$REPO" || exit 9
env -u SENTINEL_ALLOW_EXPOSURE -u SENTINEL_ALLOW_UNAUTHENTICATED_EXPOSURE \
  EDGE_SENTINEL_DEMO="$DEMO_MODE" \
  SENTINEL_BIND_HOST=127.0.0.1 \
  SENTINEL_BASIC_AUTH_USER="${CREDS%%:*}" \
  SENTINEL_BASIC_AUTH_PASS="${CREDS#*:}" \
  SENTINEL_BLOCK_ENABLED=1 \
  "$ES_PYTHON" "$REPO/scripts/es_serve_for_audit.py" > "$OUT/server.log" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT

for i in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 0.5
done
if ! curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "server did not come up:"; tail -25 "$OUT/server.log"; exit 8
fi
echo "server up on 127.0.0.1:$PORT (fixture core)"
curl -fsS "http://127.0.0.1:$PORT/health" | "$ES_PYTHON" -c "import json,sys; d=json.load(sys.stdin); print('health.demo =', d.get('demo'))"

AUDIT_OPERATOR_CREDS="$CREDS" "$UI_PYTHON" "$REPO/scripts/ui_audit_edge_sentinel.py" "http://127.0.0.1:$PORT" "$OUT" "$LABEL"
RC=$?
echo "audit exit=$RC"
exit $RC
