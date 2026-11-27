#!/bin/sh
# The finding loop on a RUNNING bundle (after ./bundle-bootstrap.sh), through the shipped surfaces only:
#   a QA run that FAILS  ->  an Edge Sentinel case  ->  a decision in Projects  ->  the re-test  ->  the case closed on a verified pass.
# The failing run uses redevops-qa's own seeded defect in its fixture application ("settings"): a known-bad build of the target. The
# remediation is the fix of that build, which here means the re-test runs the application as it is, with no seeded defect.
# Every refusal on the way is checked too: nothing to escalate from a passing run, no closing on the finding run itself, on a re-test that
# is still waiting, or on one that failed again; no closing by the service credential alone; nothing for an anonymous caller.
#   usage: ./bundle-loop.sh
set -eu
cd "$(dirname "$0")"
get() { sed -n "s/^$1=//p" .env.bundle | tail -1; }
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*" >&2; exit 1; }
json() { python3 -c 'import sys,json; d=json.load(sys.stdin); print('"$1"')'; }

PBASE="http://127.0.0.1:$(get PROJECTS_PORT)"; QBASE="http://127.0.0.1:$(get QA_PORT)"; SBASE="http://127.0.0.1:$(get EDGE_SENTINEL_PORT)"
PB="$(get OPERATOR_BASIC_AUTH)"; QT="$(get QA_SERVICE_TOKEN)"; ST="$(get EDGE_SENTINEL_SERVICE_TOKEN)"
OPUSER="$(get OPERATOR_USER)"; OPUSER="${OPUSER:-operator}"
QADMIN="admin:$(get QA_BOOTSTRAP_ADMIN_PASSWORD)"
qa() { curl -sS --max-time 60 -H "Authorization: Bearer $QT" -H 'content-type: application/json' "$@"; }
qadmin() { curl -sS --max-time 60 -u "$QADMIN" -H 'content-type: application/json' "$@"; }
sn() { curl -sS --max-time 60 -H "X-Control-Token: $ST" -H 'content-type: application/json' "$@"; }      # Sentinel's service credential header
pj() { curl -sS --max-time 60 -u "$PB" -H 'content-type: application/json' "$@"; }
pcode() { curl -s -o /dev/null -w '%{http_code}' --max-time 60 -u "$PB" -H 'content-type: application/json' "$@"; }
acode() { curl -s -o /dev/null -w '%{http_code}' --max-time 60 -H 'content-type: application/json' "$@"; }
T0="$(date +%s)"

profile="$(qa "$QBASE/v1/info" | json 'next((p["id"] for p in d["profiles"] if p.get("available") and p["id"] == "fixture-browser"), "")')"
if [ -z "$profile" ]; then
  why="$(qa "$QBASE/v1/info" | json '",".join(sorted({(v.get("unavailable") or {}).get("code","?") for v in d["plans"].values()}))')"
  echo "UNSUPPORTED  the finding loop needs redevops-qa's browser lane, which is unavailable on this host ($why). Nothing was run: this is not a pass."
  exit 0
fi
qa "$QBASE/v1/info" | json '1 if "settings" in d["defects"] else 0' | grep -q ^1$ || bad "redevops-qa does not offer the seeded defect 'settings'"

tid="$(qadmin -X POST -d '{"name":"finding-loop-'"$$"'","run_approval":"required"}' "$QBASE/v1/targets" | json 'd["id"]')"
qadmin -X POST -d '{"external_actor":"projects:'"$OPUSER"'"}' "$QBASE/v1/actor-links" >/dev/null

wait_run() { # run id -> prints the final record
  i=0; while :; do st="$(qa "$QBASE/v1/runs/$1" | json 'd["state"]')"; [ "$st" != running ] && [ "$st" != awaiting_approval ] && [ "$st" != queued ] && break
    i=$((i+1)); [ $i -gt 200 ] && bad "run $1 still $st after 600s"; sleep 3; done
  qa "$QBASE/v1/runs/$1"
}
approve() { # run id: approve THROUGH PROJECTS with the estimate digest the run carries
  est="$(qa "$QBASE/v1/runs/$1" | json 'd["estimate_digest"]')"
  c="$(pcode -X POST -d '{"approved":true,"estimate_digest":"'"$est"'","rationale":"finding loop"}' "$PBASE/api/products/redevops-qa/runs/$1/approval")"
  [ "$c" = 200 ] || bad "approving run $1 through Projects answered $c"
}
start() { # defect -> run id (the run waits for approval, bound to a fresh estimate)
  e="$(qa -X POST -d '{"target_id":"'"$tid"'","profile":"'"$profile"'"}' "$QBASE/v1/estimates" | json 'd["digest"]')"
  qa -X POST -d '{"target_id":"'"$tid"'","profile":"'"$profile"'","defect":"'"$1"'","estimate_digest":"'"$e"'"}' "$QBASE/v1/runs" | json 'd["id"]'
}
case_status() { sn "$SBASE/api/v1/cases/$1" | json 'd["summary"]["status"]'; }

# 0. nothing for an anonymous caller
for path in "runs/r20260101T000000Z-000000/escalation" "runs/r20260101T000000Z-000000/retest"; do
  [ "$(acode -X POST -d '{}' "$PBASE/api/products/redevops-qa/$path")" = 401 ] || bad "anonymous $path was not refused"
done
[ "$(acode -X POST -d '{}' "$PBASE/api/products/edge-sentinel/cases/case-0000000000000000/resolution")" = 401 ] || bad "anonymous resolution was not refused"
ok "the three routes refuse an anonymous caller (401)"

# 1. the finding: a run of the known-bad build fails, with evidence that still verifies
F="$(start settings)"; approve "$F"; rec="$(wait_run "$F")"
[ "$(echo "$rec" | json 'str(d.get("outcome"))')" = FAILED ] || bad "the defective build ended $(echo "$rec" | json 'str(d.get("outcome"))'), wanted FAILED"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $QT" "$QBASE/v1/runs/$F/verify")" = 200 ] || bad "the failed run's evidence does not verify"
ok "finding: run $F of the defective build ended FAILED and its evidence verifies"

# 2. a passing run is not a finding
G="$(start none)"; approve "$G"; rec="$(wait_run "$G")"
[ "$(echo "$rec" | json 'str(d.get("outcome"))')" = VERIFIED ] || bad "the control run ended $(echo "$rec" | json 'str(d.get("outcome"))')"
[ "$(pcode -X POST -d '{}' "$PBASE/api/products/redevops-qa/runs/$G/escalation")" = 409 ] && ok "a run that passed cannot be escalated (409)" || bad "a passing run was escalated"

# 3. escalate: the finding becomes ONE Edge Sentinel case that names the run and its evidence
out="$(pj -X POST -d '{"severity":"high"}' "$PBASE/api/products/redevops-qa/runs/$F/escalation")"
CASE="$(echo "$out" | json 'd["result"]["case_id"]')"; MAN="$(echo "$out" | json 'd["result"]["manifest_digest"]')"
[ -n "$CASE" ] && [ -n "$MAN" ] || bad "escalation returned no case or no evidence digest: $out"
c="$(sn "$SBASE/api/v1/cases/$CASE")"
echo "$c" | json '1 if d["summary"]["status"] == "INVESTIGATING" and "'"$F"'" in json.dumps(d) and "'"$MAN"'" in json.dumps(d) and d["actions_detail"] == [] else 0' | grep -q ^1$ \
  || bad "the Sentinel case does not carry the run, its evidence digest, or it proposes an action"
again="$(pj -X POST -d '{"severity":"high"}' "$PBASE/api/products/redevops-qa/runs/$F/escalation" | json 'd["result"]["case_id"]')"
[ "$again" = "$CASE" ] || bad "escalating the same run twice opened a second case"
ok "escalated through Projects -> Sentinel case $CASE (INVESTIGATING, names run and evidence $MAN, proposes no action); a second escalation is the same case"

# 4. the case cannot be closed without a re-test that passed
res() { pcode -X POST -d '{"finding_run_id":"'"$F"'","retest_run_id":"'"$1"'"}' "$PBASE/api/products/edge-sentinel/cases/$CASE/resolution"; }
[ "$(res "$F")" = 409 ] || bad "the finding run was accepted as its own re-test"
B="$(start settings)"; approve "$B"; wait_run "$B" >/dev/null
[ "$(res "$B")" = 409 ] || bad "a re-test that failed again closed the case"
dig="$(python3 -c 'import hashlib,json,sys; ev={"kind":"qa.retest","run_id":sys.argv[2],"outcome":"VERIFIED","manifest_digest":"sha256:"+"0"*64,"finding_run_id":sys.argv[3]}; print(json.dumps({"evidence":ev,"digest":hashlib.sha256(json.dumps({"case_id":sys.argv[1],"evidence":ev},sort_keys=True,separators=(",",":")).encode()).hexdigest()}))' "$CASE" "$G" "$F")"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "X-Control-Token: $ST" -H 'content-type: application/json' -X POST -d "$dig" "$SBASE/api/v1/cases/$CASE/resolution")" = 403 ] \
  || bad "the service credential alone closed a case at Sentinel"
[ "$(case_status "$CASE")" = INVESTIGATING ] || bad "the case left INVESTIGATING after refused resolutions"
ok "not closed by the finding run itself, by a re-test that failed again, or by the service credential alone; the case is still INVESTIGATING"

# 5. the decision and the re-test, through Projects
out="$(pj -X POST "$PBASE/api/products/redevops-qa/runs/$F/retest")"
R="$(echo "$out" | json 'd["result"]["retest_run_id"]')"
[ "$(echo "$out" | json 'd["result"]["state"]')" = awaiting_approval ] || bad "the re-test did not wait for approval: $out"
[ "$(qa "$QBASE/v1/runs/$R" | json 'str(d.get("defect"))')" = none ] || bad "the re-test carries a seeded defect"
[ "$(res "$R")" = 409 ] || bad "a re-test that has not run yet closed the case"
approve "$R"; rec="$(wait_run "$R")"
[ "$(echo "$rec" | json 'str(d.get("outcome"))')" = VERIFIED ] || bad "the re-test ended $(echo "$rec" | json 'str(d.get("outcome"))'), wanted VERIFIED"
ok "re-test $R requested and approved through Projects (it could not close the case while waiting) -> VERIFIED"

# 6. resolution: closed on the verified re-test, by a person, with the record kept
out="$(pj -X POST -d '{"finding_run_id":"'"$F"'","retest_run_id":"'"$R"'"}' "$PBASE/api/products/edge-sentinel/cases/$CASE/resolution")"
[ "$(echo "$out" | json 'd["result"]["case_status"]')" = RESOLVED ] || bad "resolution did not close the case: $out"
EV="$(echo "$out" | json 'd["result"]["evidence_id"]')"
[ "$(case_status "$CASE")" = RESOLVED ] || bad "Sentinel does not show the case as RESOLVED"
sn "$SBASE/api/v1/cases/$CASE/evidence/$EV" | json '1 if d["evidence"]["run_id"] == "'"$R"'" and d["evidence"]["finding_run_id"] == "'"$F"'" and d["via"] == "projects" and d["resolved_by"].startswith("user:") else 0' | grep -q ^1$ \
  || bad "the resolution record on the case does not name the re-test, the finding run and the person"
[ "$(res "$R")" = 409 ] || bad "a closed case accepted a second resolution"
ok "resolved through Projects -> case RESOLVED at Sentinel; the record names re-test $R, finding $F and the person; a second resolution is refused"

echo
echo "FINDING LOOP OK  ($(( $(date +%s) - T0 )) s; finding $F, case $CASE, re-test $R)"
