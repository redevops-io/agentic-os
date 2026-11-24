#!/bin/sh
# End-to-end check of a RUNNING single-pull bundle (after ./bundle-bootstrap.sh), through the shipped surfaces only.
# Beyond "the three interfaces load": Projects carries both products, a QA run that NEEDS APPROVAL is approved THROUGH PROJECTS (signed,
# single use, bound to the estimate the approver saw), wrong and replayed approvals are refused, and the Sentinel boundary stays closed
# to forged tokens.
#   usage: ./bundle-smoke.sh
set -eu
cd "$(dirname "$0")"
get() { sed -n "s/^$1=//p" .env.bundle | tail -1; }
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*" >&2; exit 1; }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$@"; }
json() { python3 -c 'import sys,json; d=json.load(sys.stdin); print('"$1"')'; }

PBASE="http://127.0.0.1:$(get PROJECTS_PORT)"; QBASE="http://127.0.0.1:$(get QA_PORT)"; SBASE="http://127.0.0.1:$(get EDGE_SENTINEL_PORT)"
PB="$(get OPERATOR_BASIC_AUTH)"; QT="$(get QA_SERVICE_TOKEN)"; ST="$(get EDGE_SENTINEL_SERVICE_TOKEN)"
OPUSER="$(get OPERATOR_USER)"; OPUSER="${OPUSER:-operator}"
QADMIN="admin:$(get QA_BOOTSTRAP_ADMIN_PASSWORD)"
qa() { curl -sS --max-time 60 -H "Authorization: Bearer $QT" -H 'content-type: application/json' "$@"; }
qadmin() { curl -sS --max-time 60 -u "$QADMIN" -H 'content-type: application/json' "$@"; }
pj() { curl -sS --max-time 60 -u "$PB" -H 'content-type: application/json' "$@"; }

# 1. probes and the enforced profile
[ "$(code "$SBASE/readyz")" = 200 ] && [ "$(code "$QBASE/healthz")" = 200 ] && [ "$(code "$PBASE/")" = 200 ] && ok "Edge Sentinel, redevops-qa and Projects answer" || bad "a service does not answer"
prof="$(docker compose -f "oci://ghcr.io/redevops-io/redevops-ts:$(get BUNDLE_TAG)" --project-directory . --env-file .env.bundle exec -T projects sh -c 'echo "$AGENTIC_OS_PROFILE $AGENTIC_OS_PACKAGED"' 2>/dev/null || true)"
case "$prof" in "production 1") ok "Projects runs packaged under the production profile";; "") echo "NOTE  could not read the Projects container environment";; *) [ "${SMOKE_PRE_PROFILE_BUNDLE:-}" = 1 ] && echo "NOTE  bundle predates the packaged-profile rule ('$prof')" || bad "Projects profile is '$prof', wanted 'production 1'";; esac

# 2. fail closed before identity
out="$(curl -sS "$PBASE/api/products")"
for v in "$QT" "$ST" "$(get APPROVAL_SECRET)" "${PB#*:}"; do printf '%s' "$out" | grep -qF "$v" && bad "the anonymous read surface carries a credential"; done
ok "/api/products answers anonymously without any credential value"
[ "$(code -X POST -H 'content-type: application/json' -d '{"approved":true,"estimate_digest":"00"}' "$PBASE/api/products/redevops-qa/runs/x/approval")" = 401 ] && ok "an anonymous approval is refused (401)" || bad "anonymous approval"
[ "$(code "$QBASE/v1/info")" = 401 ] && [ "$(code "$SBASE/api/v1/cases")" != 200 ] && ok "QA and Sentinel APIs refuse an anonymous caller" || bad "anonymous API access"

# 3. Projects carries both products
out="$(pj "$PBASE/api/products")"
for id in edge-sentinel redevops-qa; do
  echo "$out" | json '1 if any(p["id"]=="'"$id"'" and p.get("ready") and p.get("approval_secret") and p["capabilities"] for p in d["products"]) else 0' | grep -q ^1$ || bad "$id is not ready in Projects"
done
ok "both products ready inside Projects with capabilities and the approval secret"

# 4. a QA run that needs approval, approved THROUGH PROJECTS
profile="$(qa "$QBASE/v1/info" | json 'next((p["id"] for p in d["profiles"] if p.get("available") and p["id"].startswith("fixture-")), "")')"
[ -n "$profile" ] || bad "QA advertises no available fixture profile on this host (/v1/info says why)"
tid="$(qadmin -X POST -d '{"name":"smoke-approval-'"$$"'","run_approval":"required"}' "$QBASE/v1/targets" | json 'd["id"]')"
qadmin -X POST -d '{"external_actor":"projects:'"$OPUSER"'"}' "$QBASE/v1/actor-links" >/dev/null
est="$(qa -X POST -d '{"target_id":"'"$tid"'","profile":"'"$profile"'"}' "$QBASE/v1/estimates" | json 'd["digest"]')"
rid="$(qa -X POST -d '{"target_id":"'"$tid"'","profile":"'"$profile"'","estimate_digest":"'"$est"'"}' "$QBASE/v1/runs" | json 'd["id"]')"
[ "$(qa "$QBASE/v1/runs/$rid" | json 'd["state"]')" = awaiting_approval ] && ok "QA run $rid ($profile) waits for approval" || bad "the run did not wait for approval"
[ "$(code -H "Authorization: Bearer $QT" -H 'content-type: application/json' -X POST -d '{"approved":true,"estimate_digest":"'"$est"'"}' "$QBASE/v1/runs/$rid/approval")" = 403 ] \
  && ok "the service token alone cannot approve it" || bad "the service token approved a run"
pj "$PBASE/api/products/redevops-qa/records/$rid" | json '1' >/dev/null || bad "Projects cannot read the waiting run"
c="$(code -u "$PB" -H 'content-type: application/json' -X POST -d '{"approved":true,"estimate_digest":"0000"}' "$PBASE/api/products/redevops-qa/runs/$rid/approval")"
[ "$c" != 200 ] && [ "$(qa "$QBASE/v1/runs/$rid" | json 'd["state"]')" = awaiting_approval ] && ok "an approval for a different estimate is refused ($c) and the run still waits" || bad "a wrong-estimate approval went through"
c="$(code -u "$PB" -H 'content-type: application/json' -X POST -d '{"approved":true,"estimate_digest":"'"$est"'","rationale":"smoke"}' "$PBASE/api/products/redevops-qa/runs/$rid/approval")"
[ "$c" = 200 ] || bad "approving through Projects answered $c"
i=0; while :; do st="$(qa "$QBASE/v1/runs/$rid" | json 'd["state"]')"; [ "$st" != running ] && [ "$st" != awaiting_approval ] && break; i=$((i+1)); [ $i -gt 200 ] && bad "run $rid still $st after 600s"; sleep 3; done
rec="$(qa "$QBASE/v1/runs/$rid")"
[ "$(echo "$rec" | json 'str(d.get("outcome"))')" = VERIFIED ] || bad "run $rid ended $(echo "$rec" | json 'str(d.get("outcome"))'), wanted VERIFIED"
echo "$rec" | json '1 if "projects:'"$OPUSER"'" in json.dumps(d) else 0' | grep -q ^1$ || bad "the run does not record that it was approved from Projects"
ok "approved through Projects -> run VERIFIED, with the Projects actor on the record"
[ "$(code -u "$PB" -H 'content-type: application/json' -X POST -d '{"approved":true,"estimate_digest":"'"$est"'"}' "$PBASE/api/products/redevops-qa/runs/$rid/approval")" != 200 ] \
  && ok "the same approval cannot be used twice" || bad "a replayed approval was accepted"
[ "$(code -H "Authorization: Bearer $QT" "$QBASE/v1/runs/$rid/verify")" = 200 ] && ok "the run's evidence re-verifies" || bad "verify"

# 5. Edge Sentinel through Projects: read path works, decision paths refuse what is not a signed one-click approval
pj "$PBASE/api/products" | json '1 if any(p["id"]=="edge-sentinel" and p.get("version") for p in d["products"]) else 0' | grep -q ^1$ && ok "Projects reads Edge Sentinel's version and features" || bad "sentinel info through Projects"
[ "$(code -u "$PB" -X POST -H 'content-type: application/json' -d '{"digest":"00"}' "$PBASE/api/products/edge-sentinel/actions/nonexistent0/execute")" != 200 ] && ok "an execute without a valid approval is not accepted" || bad "unsigned execute accepted"
[ "$(code -H "Authorization: Bearer $ST" -X POST -H 'content-type: application/json' -d '{"_approval_token":"forged"}' "$SBASE/api/v1/cases/nonexistent0/actions/nonexistent0/execute")" != 200 ] && ok "Sentinel rejects a forged approval token at its own boundary" || bad "sentinel accepted a forged token"
[ "$(curl -sS --max-time 30 -H "Authorization: Bearer $ST" "$SBASE/api/v1/actor-links" | json '1 if "projects:'"$OPUSER"'" in json.dumps(d) else 0' 2>/dev/null || echo 0)" = 1 ] && ok "Sentinel knows who the Projects operator is (actor link)" || echo "NOTE  could not read Sentinel's actor links with the service token"

echo
echo "BUNDLE SMOKE OK"
