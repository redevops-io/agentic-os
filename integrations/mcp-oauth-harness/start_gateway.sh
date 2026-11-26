#!/bin/sh
# A throwaway gateway with OAuth, the MCP Streamable-HTTP server and a durable store, for the TypeScript SDK harness.
# usage: start_gateway.sh WORKTREE WORKDIR PORT    (stop: ./stop_gateway.sh PORT WORKDIR, which stops only the process this script recorded in WORKDIR/gateway.pid)
# It waits until the gateway listens, then records the listener's pid: that pid is the only thing the stop script will ever signal.
set -eu
WT="$1"; W="$2"; PORT="$3"; mkdir -p "$W"
[ -f "$W/secrets.env" ] && grep -q REQUESTER_ACME "$W/secrets.env" || python3 - "$W" <<'PY'
import secrets, sys
w = sys.argv[1]
open(w + "/secrets.env", "w").write(
    f"OPERATOR_BASIC_AUTH=operator:{secrets.token_hex(16)}\nAGENTIC_OS_APPROVAL_SECRET={secrets.token_hex(24)}\n"
    f"APPROVER_ACME=acme-{secrets.token_hex(16)}\nAPPROVER_GLOBEX=globex-{secrets.token_hex(16)}\nREQUESTER_ACME=req-acme-{secrets.token_hex(16)}\n")
PY
. "$W/secrets.env"
cd "$WT"
nohup env -i PATH=/opt/homebrew/bin:/usr/bin:/bin:$HOME/.local/bin HOME="$HOME" \
  ${GATE_ENV-PROJECTS_OPERATOR_GATE=1} OPERATOR_BASIC_AUTH="$OPERATOR_BASIC_AUTH" \
  AGENTIC_OS_OAUTH_DB="$W/oauth.db" AGENTIC_OS_OAUTH_ISSUER="http://localhost:$PORT" \
  AGENTIC_OS_APPROVAL_SECRET="$AGENTIC_OS_APPROVAL_SECRET" AGENTIC_OS_REPLAY_DB="$W/replay.db" \
  AGENT_GATEWAY_MCP=1 AGENTIC_OS_PROFILE=production AGENTIC_OS_AUTHORITY_DB="$W/authority.db" \
  AGENTIC_OS_APPROVER_TOKENS="acme=$APPROVER_ACME,globex=$APPROVER_GLOBEX" AGENTIC_OS_TENANT_TOKENS="acme=$REQUESTER_ACME" AGENTIC_OS_TENANT_LIMITS='{"acme":{"policy_refs":["projects:read"],"budget_usd":25},"globex":{"policy_refs":["projects:read"],"budget_usd":25}}' \
  PROJECTS_UI_DIR=/nonexistent \
  uv run --frozen --extra mcp python -m uvicorn --factory agentic_os.projects_api:create_app --host 127.0.0.1 --port "$PORT" > "$W/gateway.log" 2>&1 &
for i in $(seq 1 90); do
  pid="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
  [ -n "$pid" ] && { echo "$pid" > "$W/gateway.pid"; exit 0; }
  sleep 1
done
echo "the gateway did not start listening on $PORT; see $W/gateway.log" >&2; exit 1
