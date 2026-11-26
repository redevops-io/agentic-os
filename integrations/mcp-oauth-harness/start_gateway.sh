#!/bin/sh
# A throwaway gateway with OAuth, the MCP Streamable-HTTP server and a durable store, for the TypeScript SDK harness.
# usage: start_gateway.sh WORKTREE WORKDIR PORT    (stop: ./stop_gateway.sh PORT WORKDIR, which stops only the process this script recorded in WORKDIR/gateway.pid)
# It waits until the gateway listens, then records the listener's pid: that pid is the only thing the stop script will ever signal.
set -eu
# uv may live anywhere (a CI runner puts it elsewhere than a laptop): keep its directory on the minimal PATH the servers get
TOOLPATH="$(dirname "$(command -v uv)"):/opt/homebrew/bin:/usr/bin:/bin:$HOME/.local/bin"
WT="$1"; W="$2"; PORT="$3"; mkdir -p "$W"
[ -f "$W/secrets.env" ] && grep -q REQUESTER_ACME "$W/secrets.env" || python3 - "$W" <<'PY'
import secrets, sys
w = sys.argv[1]
open(w + "/secrets.env", "w").write(
    f"OPERATOR_BASIC_AUTH=operator:{secrets.token_hex(16)}\nAGENTIC_OS_APPROVAL_SECRET={secrets.token_hex(24)}\n"
    f"APPROVER_ACME=acme-{secrets.token_hex(16)}\nAPPROVER_GLOBEX=globex-{secrets.token_hex(16)}\nREQUESTER_ACME=req-acme-{secrets.token_hex(16)}\nPRODUCT_SERVICE_TOKEN=svc-{secrets.token_hex(24)}\n")
PY
. "$W/secrets.env"
PRODUCTS_ENV=""; PRODUCT_LIMITS='{"acme":{"policy_refs":["projects:read"],"budget_usd":25},"globex":{"policy_refs":["projects:read"],"budget_usd":25}}'
if [ -f "$WT/agentic_os/agent_gateway/product_capabilities.py" ] && [ -d "$WT/apps/edge-sentinel" ]; then
  # the checkout exposes product tools: run a real Edge Sentinel (demo fixture) beside the gateway and register it; tenant acme is granted the product, globex is not
  SPORT=$((PORT + 1)); mkdir -p "$W/pkg" "$W/sentinel-state"; ln -sfn "$WT/apps/edge-sentinel" "$W/pkg/edge_sentinel"
  printf '%s' "$PRODUCT_SERVICE_TOKEN" > "$W/product-service-token"; printf '%s' "$AGENTIC_OS_APPROVAL_SECRET" > "$W/product-approval-secret"; chmod 600 "$W/product-service-token" "$W/product-approval-secret"
  cat > "$W/products.yaml" <<YAML
schema: aos.products/v1
products:
  - id: edge-sentinel
    display_name: Edge Sentinel
    kind: security
    api_base: http://127.0.0.1:$SPORT
    ui_url: http://127.0.0.1:$SPORT
    health: {path: /readyz, interval_s: 5, timeout_s: 3}
    contract: {api: /api/v1, info: /api/v1/info, min_version: "1"}
    auth: {service_token_file: $W/product-service-token, approval_secret_file: $W/product-approval-secret, scheme: control_token}
YAML
  ( cd "$W/pkg" && nohup env -i PATH="$TOOLPATH" HOME="$HOME" PYTHONPATH="$W/pkg" EDGE_SENTINEL_DEMO=1 EDGE_SENTINEL_DEMO_NOW=2026-11-26T12:00:00Z SENTINEL_SYNC_INTERVAL_S=5 \
      SENTINEL_STATE_DIR="$W/sentinel-state" SENTINEL_BASIC_AUTH_USER=sentinel-admin SENTINEL_BASIC_AUTH_PASS=demo-admin-pass-1 AGENTIC_OS_CONTROL_TOKEN="$PRODUCT_SERVICE_TOKEN" AGENTIC_OS_APPROVAL_SECRET="$AGENTIC_OS_APPROVAL_SECRET" PORT="$SPORT" \
      uv run --frozen --project "$WT" python -m uvicorn edge_sentinel.app:app --host 127.0.0.1 --port "$SPORT" > "$W/sentinel.log" 2>&1 < /dev/null & )
  PRODUCTS_ENV="AGENTIC_OS_PRODUCTS=$W/products.yaml"
  PRODUCT_LIMITS='{"acme":{"policy_refs":["projects:read","products:edge-sentinel"],"budget_usd":25},"globex":{"policy_refs":["projects:read"],"budget_usd":25}}'
  for i in $(seq 1 90); do spid="$(lsof -ti "tcp:$SPORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"; [ -n "$spid" ] && break; sleep 1; done
  [ -n "${spid:-}" ] && echo "$spid" > "$W/sentinel.pid"
fi
cd "$WT"
nohup env -i PATH="$TOOLPATH" HOME="$HOME" \
  ${GATE_ENV-PROJECTS_OPERATOR_GATE=1} OPERATOR_BASIC_AUTH="$OPERATOR_BASIC_AUTH" \
  AGENTIC_OS_OAUTH_DB="$W/oauth.db" AGENTIC_OS_OAUTH_ISSUER="http://localhost:$PORT" \
  AGENTIC_OS_APPROVAL_SECRET="$AGENTIC_OS_APPROVAL_SECRET" AGENTIC_OS_REPLAY_DB="$W/replay.db" \
  AGENT_GATEWAY_MCP=1 AGENTIC_OS_PROFILE=production AGENTIC_OS_AUTHORITY_DB="$W/authority.db" \
  AGENTIC_OS_APPROVER_TOKENS="acme=$APPROVER_ACME,globex=$APPROVER_GLOBEX" AGENTIC_OS_TENANT_TOKENS="acme=$REQUESTER_ACME" AGENTIC_OS_TENANT_LIMITS="$PRODUCT_LIMITS" $PRODUCTS_ENV \
  PROJECTS_UI_DIR=/nonexistent \
  uv run --frozen --extra mcp python -m uvicorn --factory agentic_os.projects_api:create_app --host 127.0.0.1 --port "$PORT" > "$W/gateway.log" 2>&1 < /dev/null &
for i in $(seq 1 90); do
  pid="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
  [ -n "$pid" ] && { echo "$pid" > "$W/gateway.pid"; exit 0; }
  sleep 1
done
echo "the gateway did not start listening on $PORT; its log:" >&2; tail -60 "$W/gateway.log" >&2 || true; exit 1
