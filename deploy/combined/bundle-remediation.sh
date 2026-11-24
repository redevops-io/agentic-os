#!/bin/sh
# A REAL remediation on a running bundle (after ./bundle-bootstrap.sh), through the shipped surfaces only:
#   redevops-qa finds that an address which must be blocked is NOT blocked at the edge  ->  the finding becomes an Edge Sentinel case
#   ->  a person proposes the edge block in Projects, approves it and executes it  ->  Edge Sentinel writes a real CrowdSec decision and
#   reads it back  ->  redevops-qa runs the SAME plan again against the SAME instance and now passes  ->  the case is closed on that run.
# Nothing is switched between fixtures: the first and the second run have the same plan digest, the same target and no seeded defect.
# What changed in between is the state of the edge, and CrowdSec itself is asked to confirm it.
#
# The address is 203.0.113.7 (RFC 5737, documentation): banning it affects nobody. For this the script sets three switches in
# .env.bundle and says so: SENTINEL_BLOCK_ENABLED=1 (approved bans are executed, not dry runs), SENTINEL_BLOCK_ALLOW_NETWORKS=203.0.113.0/24
# (the documentation range may be banned) and SENTINEL_ENFORCEMENT_PROBE=1 (the read-only page QA reads). It also starts the
# `remediation` profile: a forwarder that lets redevops-qa reach Edge Sentinel on its own loopback, the only place it accepts a target.
#   usage: ./bundle-remediation.sh
set -eu
cd "$(dirname "$0")"
get() { sed -n "s/^$1=//p" .env.bundle | tail -1; }
set_in_env() { if grep -q "^$1=" .env.bundle; then sed -i.bak "s|^$1=.*|$1=$2|" .env.bundle && rm -f .env.bundle.bak; else echo "$1=$2" >> .env.bundle; fi; }
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*" >&2; exit 1; }
json() { python3 -c 'import sys,json; d=json.load(sys.stdin); print('"$1"')'; }

IP=203.0.113.7
PROFILE=edge-sentinel-enforcement
BUNDLE_REF="ghcr.io/redevops-io/redevops-ts:$(get BUNDLE_TAG || true)"; BUNDLE_REF="${BUNDLE_REF%:}"
[ "$BUNDLE_REF" = "ghcr.io/redevops-io/redevops-ts:" ] && BUNDLE_REF="ghcr.io/redevops-io/redevops-ts:0.1.8"
COMPOSE="${BUNDLE_COMPOSE:-docker compose -f oci://${BUNDLE_REF} --project-directory . --env-file .env.bundle}"
PBASE="http://127.0.0.1:$(get PROJECTS_PORT)"; QBASE="http://127.0.0.1:$(get QA_PORT)"; SBASE="http://127.0.0.1:$(get EDGE_SENTINEL_PORT)"
PB="$(get OPERATOR_BASIC_AUTH)"; QT="$(get QA_SERVICE_TOKEN)"; ST="$(get EDGE_SENTINEL_SERVICE_TOKEN)"
OPUSER="$(get OPERATOR_USER)"; OPUSER="${OPUSER:-operator}"
QADMIN="admin:$(get QA_BOOTSTRAP_ADMIN_PASSWORD)"
qa() { curl -sS --max-time 60 -H "Authorization: Bearer $QT" -H 'content-type: application/json' "$@"; }
qadmin() { curl -sS --max-time 60 -u "$QADMIN" -H 'content-type: application/json' "$@"; }
sn() { curl -sS --max-time 60 -H "X-Control-Token: $ST" -H 'content-type: application/json' "$@"; }
pj() { curl -sS --max-time 60 -u "$PB" -H 'content-type: application/json' "$@"; }
pcode() { curl -s -o /dev/null -w '%{http_code}' --max-time 60 -u "$PB" -H 'content-type: application/json' "$@"; }
acode() { curl -s -o /dev/null -w '%{http_code}' --max-time 60 -H 'content-type: application/json' "$@"; }
banned() { $COMPOSE exec -T crowdsec cscli decisions list --ip "$IP" -o json < /dev/null 2>/dev/null | python3 -c 'import sys,json
try: d=json.load(sys.stdin) or []
except Exception: d=[]
print("yes" if any((x.get("value")=="'"$IP"'" and x.get("type")=="ban") for a in d for x in (a.get("decisions") or [])) else "no")'; }
page() { curl -s --max-time 20 "$SBASE/enforcement/$IP" | sed -n 's/.*data-testid="enforcement-state">\([^<]*\)<.*/\1/p'; }
T0="$(date +%s)"

# 0. the browser lane must exist here; if it does not, nothing is run and nothing is claimed
info="$(qa "$QBASE/v1/info")"
if [ "$(echo "$info" | json 'next(("yes" for p in d["profiles"] if p["id"] == "'"$PROFILE"'"), "no")')" != yes ]; then
  bad "this redevops-qa does not have the $PROFILE profile (it needs redevops-qa 0.6.5 or later)"
fi
if [ "$(echo "$info" | json 'next(("yes" for p in d["profiles"] if p["id"] == "'"$PROFILE"'" and p.get("available")), "no")')" != yes ]; then
  why="$(echo "$info" | json 'next(((p.get("unavailable") or {}).get("code","?") for p in d["profiles"] if p["id"] == "'"$PROFILE"'"), "?")')"
  echo "UNSUPPORTED  the remediation re-test needs redevops-qa's browser lane, which is unavailable on this host ($why). Nothing was run: this is not a pass."
  exit 0
fi

# 1. the three switches and the forwarder (stated, not silent)
for kv in SENTINEL_BLOCK_ENABLED=1 SENTINEL_BLOCK_ALLOW_NETWORKS=203.0.113.0/24 SENTINEL_ENFORCEMENT_PROBE=1; do
  [ "$(get "${kv%%=*}")" = "${kv#*=}" ] || { set_in_env "${kv%%=*}" "${kv#*=}"; echo "NOTE  set ${kv} in .env.bundle"; }
done
$COMPOSE --profile remediation up -d -y --wait --wait-timeout 180 edge-sentinel qa-sentinel-loopback >/dev/null || bad "starting Edge Sentinel with the enforcement page, or the forwarder, failed ($COMPOSE ps)"
i=0; until [ "$(curl -s --max-time 5 "$SBASE/health" | json 'd.get("enforcement_probe",{}).get("enabled")' 2>/dev/null)" = True ]; do
  i=$((i+1)); [ $i -gt 60 ] && bad "Edge Sentinel does not report enforcement_probe.enabled=true (it needs Edge Sentinel 0.6.6 or later)"; sleep 2; done
[ "$(curl -s --max-time 5 "$SBASE/health" | json 'd["demo"]["enabled"]')" = False ] || bad "Edge Sentinel is in fixture mode: there is no edge to remediate"
ok "Edge Sentinel is a live instance (not fixture mode) with blocking enabled and its read-only enforcement page on"

# 2. a clean start: the address is not banned (set-up only; the remediation below goes through the products)
$COMPOSE exec -T crowdsec cscli decisions delete --ip "$IP" < /dev/null >/dev/null 2>&1 || true
[ "$(banned)" = no ] || bad "$IP is still banned in CrowdSec before the run"
[ "$(page)" = "not blocked" ] || bad "the enforcement page says '$(page)' before the run, wanted 'not blocked'"
[ "$(acode "$SBASE/enforcement/8.8.8.8")" = 404 ] || bad "the enforcement page answered for a real address"
ok "before: CrowdSec holds no ban for $IP and the page says 'not blocked'; the page refuses a real address (404)"

# 3. the target: redevops-qa registers the live instance by what its /health says
tid="$(qadmin "$QBASE/v1/targets" | json 'next((t["id"] for t in d["items"] if t.get("origin") == "http://127.0.0.1:8203" and t.get("presentation") == "enforcement-probe"), "")')"
if [ -z "$tid" ]; then
  out="$(qadmin -X POST -d '{"name":"edge-sentinel-live-'"$$"'","origin":"http://127.0.0.1:8203"}' "$QBASE/v1/targets")"
  tid="$(echo "$out" | json 'd.get("id","")')"; [ -n "$tid" ] || bad "redevops-qa refused the live Edge Sentinel as a target: $out"
fi
qadmin -X POST -d '{"external_actor":"projects:'"$OPUSER"'"}' "$QBASE/v1/actor-links" >/dev/null
[ "$(qadmin "$QBASE/v1/targets" | json 'next((t["level_ceiling"]+"/"+t["run_approval"] for t in d["items"] if t["id"] == "'"$tid"'"), "")')" = "CORROBORATED/required" ] \
  || bad "the live target is not capped at CORROBORATED with approval required"
ok "target $tid: the live Edge Sentinel, registered as enforcement-probe (ceiling CORROBORATED, every run needs approval)"

wait_run() { i=0; while :; do st="$(qa "$QBASE/v1/runs/$1" | json 'd["state"]')"; [ "$st" != running ] && [ "$st" != awaiting_approval ] && [ "$st" != queued ] && break
    i=$((i+1)); [ $i -gt 200 ] && bad "run $1 still $st after 600s"; sleep 3; done; qa "$QBASE/v1/runs/$1"; }
approve() { est="$(qa "$QBASE/v1/runs/$1" | json 'd["estimate_digest"]')"
  c="$(pcode -X POST -d '{"approved":true,"estimate_digest":"'"$est"'","rationale":"remediation loop"}' "$PBASE/api/products/redevops-qa/runs/$1/approval")"
  [ "$c" = 200 ] || bad "approving run $1 through Projects answered $c"; }
plan_digest() { qa -X POST -d '{"target_id":"'"$tid"'","profile":"'"$PROFILE"'"}' "$QBASE/v1/estimates" | json 'd["plan_digest"]'; }
start() { e="$(qa -X POST -d '{"target_id":"'"$tid"'","profile":"'"$PROFILE"'"}' "$QBASE/v1/estimates")"
  qa -X POST -d '{"target_id":"'"$tid"'","profile":"'"$PROFILE"'","estimate_digest":"'"$(echo "$e" | json 'd["digest"]')"'"}' "$QBASE/v1/runs" | json 'd["id"]'; }
verify_ok() { [ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $QT" "$QBASE/v1/runs/$1/verify")" = 200 ]; }
case_status() { sn "$SBASE/api/v1/cases/$1" | json 'd["summary"]["status"]'; }

# 4. the finding: the real instance does not block the address
PLAN_F="$(plan_digest)"; F="$(start)"; approve "$F"; rec="$(wait_run "$F")"
[ "$(echo "$rec" | json 'str(d.get("outcome"))')" = FAILED ] || bad "the run against the unblocked edge ended $(echo "$rec" | json 'str(d.get("outcome"))'), wanted FAILED"
[ "$(echo "$rec" | json 'str(d.get("defect"))')" = none ] || bad "the finding run carries a seeded defect"
verify_ok "$F" || bad "the failed run's evidence does not verify"
ok "finding: run $F (no seeded defect, plan $PLAN_F) ended FAILED against the live edge and its evidence verifies"

# 5. escalate through Projects
out="$(pj -X POST -d '{"severity":"high"}' "$PBASE/api/products/redevops-qa/runs/$F/escalation")"
CASE="$(echo "$out" | json 'd["result"]["case_id"]')"; [ -n "$CASE" ] || bad "escalation returned no case: $out"
[ "$(case_status "$CASE")" = INVESTIGATING ] || bad "the case is not INVESTIGATING"
ok "escalated through Projects -> Edge Sentinel case $CASE (INVESTIGATING)"

# 6. refusals around the remedy
rem() { pcode -X POST -d "$1" "$PBASE/api/products/edge-sentinel/cases/$CASE/remedy"; }
[ "$(acode -X POST -d '{"type":"edge_block","finding_run_id":"'"$F"'","ip":"'"$IP"'"}' "$PBASE/api/products/edge-sentinel/cases/$CASE/remedy")" = 401 ] || bad "an anonymous remedy was not refused"
[ "$(rem '{"type":"edge_block","finding_run_id":"r20260101T000000Z-000000","ip":"'"$IP"'"}')" = 409 ] || bad "a remedy naming another run was accepted"
[ "$(rem '{"type":"edge_block","finding_run_id":"'"$F"'","ip":"10.1.2.3"}')" != 200 ] || bad "a remedy for a private address was staged"
[ "$(rem '{"type":"edge_block","finding_run_id":"'"$F"'","ip":"203.0.113.0/24"}')" = 422 ] || bad "a remedy for a range was accepted"
[ "$(banned)" = no ] || bad "a refused remedy changed the edge"
ok "no remedy for an anonymous caller (401), for another run (409), for a private address or for a range; the edge is unchanged"

# 7. the remedy is proposed: staged only
out="$(pj -X POST -d '{"type":"edge_block","finding_run_id":"'"$F"'","ip":"'"$IP"'","duration_hours":1}' "$PBASE/api/products/edge-sentinel/cases/$CASE/remedy")"
REQ="$(echo "$out" | json 'd["result"]["request_id"]')"; DIG="$(echo "$out" | json 'd["result"]["digest"]')"
[ -n "$REQ" ] && [ "$(echo "$out" | json 'd["result"]["state"]')" = AWAITING_APPROVAL ] || bad "the remedy was not staged: $out"
[ "$(banned)" = no ] && [ "$(page)" = "not blocked" ] || bad "proposing the remedy already changed the edge"
[ "$(pcode -X POST -d '{"digest":"'"$DIG"'"}' "$PBASE/api/products/edge-sentinel/actions/$REQ/execute")" = 409 ] || bad "an action that was not approved was executed"
[ "$(banned)" = no ] || bad "an unapproved execute changed the edge"
ok "remedy proposed through Projects: action $REQ is AWAITING_APPROVAL; nothing is banned, and executing it unapproved is refused (409)"

# 8. a re-test BEFORE the remediation fails again and cannot close the case
res() { pcode -X POST -d '{"finding_run_id":"'"$F"'","retest_run_id":"'"$1"'"}' "$PBASE/api/products/edge-sentinel/cases/$CASE/resolution"; }
B="$(pj -X POST "$PBASE/api/products/redevops-qa/runs/$F/retest" | json 'd["result"]["retest_run_id"]')"; approve "$B"; rec="$(wait_run "$B")"
[ "$(echo "$rec" | json 'str(d.get("outcome"))')" = FAILED ] || bad "a re-test before the remediation ended $(echo "$rec" | json 'str(d.get("outcome"))')"
[ "$(res "$B")" = 409 ] || bad "a re-test that failed again closed the case"
ok "a re-test before the remediation ($B) fails again and does not close the case (409)"

# 9. the decision and the execution, two separate steps by a person, through Projects
[ "$(pcode -X POST -d '{"approved":true,"digest":"'"$(printf %064d 0)"'"}' "$PBASE/api/products/edge-sentinel/actions/$REQ/decision")" = 409 ] || bad "an approval bound to another digest was accepted"
c="$(pcode -X POST -d '{"approved":true,"digest":"'"$DIG"'","rationale":"block the address the finding names"}' "$PBASE/api/products/edge-sentinel/actions/$REQ/decision")"
[ "$c" = 200 ] || bad "approving the remedy through Projects answered $c"
[ "$(banned)" = no ] || bad "approving alone changed the edge"
out="$(pj -X POST -d '{"digest":"'"$DIG"'"}' "$PBASE/api/products/edge-sentinel/actions/$REQ/execute")"
[ "$(echo "$out" | json 'str(d["result"]["executed"])+"/"+str(d["result"]["dry_run"])')" = True/False ] || bad "the remedy was not executed for real: $out"
[ "$(echo "$out" | json '(d["result"].get("verification") or {}).get("state")')" = verified ] || bad "Edge Sentinel did not verify its own write: $out"
ok "approved, then executed through Projects: executed for real (not a dry run), and Edge Sentinel's read-back verified the ban"

# 10. the edge really changed: CrowdSec itself, and the page
[ "$(banned)" = yes ] || bad "CrowdSec does not list a ban for $IP after the execution"
[ "$(page)" = blocked ] || bad "the enforcement page says '$(page)' after the execution"
ok "after: CrowdSec's own decision list holds a ban for $IP (asked with cscli), and the page says 'blocked'"

# 11. the re-test: same target, same profile, same plan, no defect
out="$(pj -X POST "$PBASE/api/products/redevops-qa/runs/$F/retest")"
R="$(echo "$out" | json 'd["result"]["retest_run_id"]')"
[ "$(res "$R")" = 409 ] || bad "a re-test that has not run yet closed the case"
approve "$R"; rec="$(wait_run "$R")"
[ "$(echo "$rec" | json 'str(d.get("outcome"))')" = CORROBORATED ] || bad "the re-test ended $(echo "$rec" | json 'str(d.get("outcome"))'), wanted CORROBORATED"
[ "$(echo "$rec" | json 'str(d.get("defect"))+"/"+d["target_id"]+"/"+d["profile"]')" = "none/$tid/$PROFILE" ] || bad "the re-test is not the same target and profile without a defect"
PLAN_R="$(plan_digest)"
[ "$PLAN_R" = "$PLAN_F" ] || bad "the plan digest changed between the finding and the re-test ($PLAN_F -> $PLAN_R)"
verify_ok "$R" || bad "the re-test's evidence does not verify"
ok "re-test $R: same target, same profile, same plan digest, no seeded defect -> CORROBORATED (the ceiling for an external target), evidence verifies"

# 12. resolution, by a person, on that run
out="$(pj -X POST -d '{"finding_run_id":"'"$F"'","retest_run_id":"'"$R"'"}' "$PBASE/api/products/edge-sentinel/cases/$CASE/resolution")"
[ "$(echo "$out" | json 'd["result"]["case_status"]')" = RESOLVED ] || bad "resolution did not close the case: $out"
[ "$(case_status "$CASE")" = RESOLVED ] || bad "Edge Sentinel does not show the case as RESOLVED"
[ "$(res "$R")" = 409 ] || bad "a closed case accepted a second resolution"
ok "case $CASE RESOLVED at Edge Sentinel on re-test $R; a second resolution is refused"

echo
echo "REMEDIATION LOOP OK  ($(( $(date +%s) - T0 )) s; finding $F, case $CASE, remedy $REQ, re-test $R)"
echo "The ban on $IP (a documentation address) expires by itself in 1 hour. To lift it now: $COMPOSE exec -T crowdsec cscli decisions delete --ip $IP"
