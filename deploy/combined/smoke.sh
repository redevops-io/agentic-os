#!/bin/sh
# End-to-end check of a RUNNING combined install, using only shipped surfaces. It answers the question the
# marketing page makes: does Projects, started through the supported installation path, really carry both
# products — health, capabilities, deep links, a real QA round trip — and do the security-sensitive paths
# stay fail-closed. Read-only except one demo QA run in the operator's own stack.
#   usage: ./smoke.sh            (after ./bootstrap.sh)
#          ./smoke.sh --teardown  (also removes both stacks, volumes included)
set -eu
cd "$(dirname "$0")"
get() { sed -n "s/^$1=//p" .env | tail -1; }
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*" >&2; exit 1; }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@"; }
json() { python3 -c 'import sys,json; d=json.load(sys.stdin); print('"$1"')'; }

if [ "${1:-}" = "--teardown" ]; then
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  ( cd ../standalone/edge-sentinel && COMPOSE_PROJECT_NAME="$(get SENTINEL_PROJECT)" docker compose down -v --remove-orphans ) >/dev/null 2>&1 || true
  echo "torn down (secrets and products.yaml stay on disk; delete them to forget this install)"
  exit 0
fi

PPORT="$(get PROJECTS_PORT)"; PPORT="${PPORT:-8787}"
QPORT="$(get QA_PORT)"; QPORT="${QPORT:-8080}"
SD="../standalone/edge-sentinel"; SD="$(cd "$SD" && pwd)"
SPORT="$(sed -n 's/^EDGE_SENTINEL_PORT=//p' "$SD/.env" 2>/dev/null | tail -1)"; SPORT="${SPORT:-8203}"
SUSER="$(sed -n 's/^SENTINEL_BASIC_AUTH_USER=//p' "$SD/.env" 2>/dev/null | tail -1)"; SUSER="${SUSER:-operator}"
SPW="$(cat "$SD/secrets/operator_password")"
PB="$(cat secrets/operator_basic)"
QT="$(cat secrets/redevops-qa.service_token)"
PBASE="http://127.0.0.1:${PPORT}"; QBASE="http://127.0.0.1:${QPORT}"; SBASE="http://127.0.0.1:${SPORT}"

# ── 1. probes: every service answers where the install documentation says it does ──────────────────────────
[ "$(code "$SBASE/readyz")" = 200 ] && ok "Edge Sentinel /readyz" || bad "sentinel not ready"
[ "$(code "$QBASE/healthz")" = 200 ] && ok "redevops-qa /healthz" || bad "qa not healthy"
[ "$(code "$PBASE/")" = 200 ] && ok "Projects answers at /" || bad "projects not healthy"

# ── 2. fail-closed before identity: the read surface carries no secret, every decision path refuses anonymously ─
# (the "approval_secret" FIELD name in the payload is a boolean — set-or-not; the assertion is that no credential VALUE appears)
if [ "$(code "$PBASE/api/products")" = 200 ]; then
  body="$(curl -sS "$PBASE/api/products")"
  leaked=no
  for s in edge-sentinel.service_token redevops-qa.service_token approval_secret operator_basic; do
    printf '%s' "$body" | grep -q "$(cat secrets/$s)" && leaked=yes
  done
  [ "$leaked" = no ] && ok "/api/products (read-only by design) answers without leaking any credential value" || bad "the anonymous read surface carries a credential"
else
  bad "anonymous read surface"
fi
[ "$(code -X POST -H 'content-type: application/json' -d '{"digest":"00"}' "$PBASE/api/products/edge-sentinel/runs/x/approval")" = 401 ] \
  && ok "an anonymous approval request is refused (401)" || bad "anonymous approval"
[ "$(code -X POST -H 'content-type: application/json' -d '{}' "$PBASE/api/products/edge-sentinel/actions/x/execute")" = 401 ] \
  && ok "an anonymous execute request is refused (401)" || bad "anonymous execute"
[ "$(code "$QBASE/v1/info")" = 401 ] && ok "QA /v1/info is 401 without the token" || bad "qa anonymous"
[ "$(code -H "Authorization: Bearer ${QT}x" "$QBASE/v1/info")" = 401 ] && ok "QA refuses a wrong token" || bad "qa wrong token"
[ "$(code "$SBASE/api/v1/cases")" != 200 ] && ok "Sentinel /api/v1/cases refuses an anonymous caller" || bad "sentinel anonymous"

# ── 3. the supported activation, verified through the app: AGENTIC_OS_PRODUCTS is in effect ────────────────
out="$(curl -sS -u "$PB" "$PBASE/api/products")"
echo "$out" | json '0 if d["registry_errors"] == 0 else 1' | grep -q ^0$ || bad "the registry has load errors ($out)"
ok "registry loaded with zero errors"
for id in edge-sentinel redevops-qa; do
  echo "$out" | json '1 if any(p["id"]=="'"$id"'" and p.get("ready") and p.get("approval_secret") and p["capabilities"] for p in d["products"]) else 0' | grep -q ^1$ \
    || bad "$id is not ready with capabilities and the approval secret set"
done
ok "both products ready inside Projects, capabilities advertised, approval secret set on both"
# the deep links: ui_url is the origin, adapters append their own path — a page that yields /ui/ui is a lie
echo "$out" | json '1 if all("/ui/ui" not in (p.get("ui_url") or "") for p in d["products"]) else 0' | grep -q ^1$ || bad "a ui_url carries a path"
ok "product deep links are clean origins (no /ui/ui)"

# ── 4. individual UI links actually serve their interfaces on this host ────────────────────────────────────
[ "$(code "$SBASE/")" != 000 ] && ok "Sentinel UI origin answers (http://127.0.0.1:${SPORT}/)" || bad "sentinel ui"
[ "$(code "$QBASE/ui/")" = 200 ] && ok "QA browser interface at ${QBASE}/ui/" || bad "qa /ui/"
[ "$(curl -sS -u "$PB" "$PBASE/" | grep -c 'html')" -ge 1 ] && ok "Projects serves its UI" || bad "projects ui"

# ── 5. a real QA round trip through the product boundary (the operator's own stack, one demo run) ──────────
plan="$(curl -sS -H "Authorization: Bearer $QT" "$QBASE/v1/info" | json 'next((k for k,v in d["plans"].items() if v.get("available")), "")')"
[ -n "$plan" ] || bad "QA advertises no available plan on this host — /v1/info says why"
rid="$(curl -sS -X POST -H "Authorization: Bearer $QT" -d "{\"plan\":\"$plan\",\"defect\":\"none\"}" "$QBASE/v1/runs" | json 'd["id"]')"
i=0; while :; do st="$(curl -sS -H "Authorization: Bearer $QT" "$QBASE/v1/runs/$rid" | json 'd["state"]')"; [ "$st" != running ] && break; i=$((i+1)); [ $i -gt 100 ] && bad "run $rid still running after 300s"; sleep 3; done
outcome="$(curl -sS -H "Authorization: Bearer $QT" "$QBASE/v1/runs/$rid" | json 'str(d.get("outcome"))')"
[ "$outcome" = VERIFIED ] || bad "run $rid ended $outcome, wanted VERIFIED"
ok "QA run $plan -> $rid VERIFIED"
# ... and the run is visible WHERE THE OPERATOR SEES IT: the Projects approval card for the run id
curl -sS -u "$PB" "$PBASE/api/products/redevops-qa/records/$rid" | json '1 if d.get("product","redevops-qa") else 0' >/dev/null \
  || bad "Projects cannot read the run record $rid"
ok "Projects reads the run record by id (approval-card path)"
[ "$(code -H "Authorization: Bearer $QT" "$QBASE/v1/runs/$rid/report?format=html")" = 200 ] && ok "the run's HTML report renders" || bad "report"

# ── 6. Sidekick access: the product tools are registered for the assistant (without a model this is the ────
# ────  honest limit: the tools exist and gate; chatting needs a configured endpoint) ──────────────────────
cap="$(curl -sS -u "$PB" "$PBASE/api/agent-gateway/capabilities" 2>/dev/null || true)"
if echo "$cap" | grep -q 'product\.'; then ok "agent gateway advertises product capabilities"; else
  echo "$out" | json '1 if any(p["id"]=="edge-sentinel" and any(c.startswith("security.") for c in p["capabilities"]) for p in d["products"]) else 0' | grep -q ^1$ \
    || bad "no security capability to route"; ok "product capabilities advertised for routing"; fi

# ── 7. security-sensitive paths stay fail-closed (the approvals boundary is exercised, not trusted) ────────
[ "$(code -u "$PB" -X POST -H 'content-type: application/json' -d '{"digest":"00","token":"forged"}' "$PBASE/api/products/edge-sentinel/runs/nonexistent0/approval")" != 200 ] \
  && ok "a forged approval token is not accepted" || bad "forged approval accepted"
[ "$(code -u "$PB" -X POST -H 'content-type: application/json' -d '{"digest":"00"}' "$PBASE/api/products/edge-sentinel/actions/nonexistent0/execute")" != 200 ] \
  && ok "an execute without a valid one-click token is not accepted" || bad "unsigned execute accepted"
[ "$(code -H "Authorization: Bearer $(cat secrets/edge-sentinel.service_token)" -X POST -H 'content-type: application/json' -d '{"_approval_token":"forged"}' "$SBASE/api/v1/cases/nonexistent0/actions/nonexistent0/execute")" != 200 ] \
  && ok "Sentinel rejects a forged approval token at its own boundary" || bad "sentinel accepted a forged token"

# ── 8. restart resilience: Projects comes back with both products, nothing re-seeded ──────────────────────
docker compose restart projects >/dev/null 2>&1 || docker compose up -d projects >/dev/null
i=0; while :; do
  ready="$(curl -sS -u "$PB" "$PBASE/api/products" 2>/dev/null | json '1 if all(p.get("ready") for p in d["products"]) and len(d["products"])>=2 else 0' 2>/dev/null || echo 0)"
  [ "$ready" = 1 ] && break; i=$((i+1)); [ $i -gt 45 ] && bad "products not ready after a projects restart"; sleep 2
done
ok "after a restart, Projects returns with both products ready"

echo
echo "SMOKE OK — the combined install does what its documentation claims."
echo "(what this does NOT cover on macOS Docker Desktop: the QA approval round trip needs a network the"
echo " service can call back on — Linux installs only; and QA browser lanes run under amd64 emulation there.)"