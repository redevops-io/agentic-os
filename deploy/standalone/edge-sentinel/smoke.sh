#!/bin/sh
# End-to-end check of a RUNNING Compose deployment, using only shipped features:
#   probes -> every non-probe route refuses an anonymous request -> a detection (a CrowdSec alert) becomes a case ->
#   the operator approves and executes -> a receipt and a verification are returned -> the container is restarted and the
#   decision and case status are still there.
# It adds ONE test alert/decision for an address in 203.0.113.150-249 (documentation range; SMOKE_IP overrides) to CrowdSec. With SENTINEL_BLOCK_ENABLED unset the
# execution is a dry run and nothing is banned; with it set (and SENTINEL_BLOCK_ALLOW_NETWORKS covering 203.0.113.0/24) the
# ban is real, and is lifted again at the end.
#   usage: ./smoke.sh            (from this directory, after ./bootstrap.sh)
# The same script checks a Kubernetes deployment (verify-k3s.sh sets these): SMOKE_BASE (URL), SMOKE_USER, SMOKE_PASSWORD,
# SMOKE_CROWDSEC (a command prefix that runs `cscli`, e.g. "kubectl -n edge-sentinel exec deploy/crowdsec --") and
# SMOKE_RESTART (a command that restarts Sentinel and returns when it is back).
set -eu
cd "$(dirname "$0")"
PORT="$(sed -n 's/^EDGE_SENTINEL_PORT=//p' .env 2>/dev/null | tail -1)"; PORT="${PORT:-8203}"
USER_="${SMOKE_USER:-$(sed -n 's/^SENTINEL_BASIC_AUTH_USER=//p' .env 2>/dev/null | tail -1)}"; USER_="${USER_:-operator}"
BASE="${SMOKE_BASE:-http://127.0.0.1:${PORT}}"
PW="${SMOKE_PASSWORD:-$(cat secrets/operator_password)}"
CSCLI="${SMOKE_CROWDSEC:-docker compose exec -T crowdsec}"
RESTART="${SMOKE_RESTART:-docker compose restart edge-sentinel}"
# a fresh address per run (documentation range): a re-run against the same stack opens a NEW case instead of meeting the previous run's
IP="${SMOKE_IP:-203.0.113.$((150 + $(od -An -N1 -tu1 /dev/urandom | tr -d ' ') % 100))}"
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*" >&2; exit 1; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
auth() { curl -s -u "${USER_}:${PW}" "$@"; }

[ "$(code "$BASE/health")" = 200 ] && ok "/health is public and answers" || bad "/health"
[ "$(code "$BASE/readyz")" = 200 ] && ok "/readyz reports ready" || bad "/readyz"
for p in /api/activity /api/cases /api/v1/cases /api/v1/info /capabilities /openapi.json; do
  [ "$(code "$BASE$p")" = 401 ] || bad "anonymous GET $p was not refused"
done
ok "anonymous GET is refused (401) on /api/activity, /api/cases, /api/v1/cases, /api/v1/info, /capabilities, /openapi.json"
c="$(code "$BASE/")"; [ "$c" = 401 ] || [ "$c" = 302 ] && ok "the front page only redirects to the console (or asks for sign-in): $c" || bad "GET / answered $c"
[ "$(code "$BASE/ui/")" = 200 ] && ok "the console's static page loads before sign-in and carries no data" || bad "/ui/"
[ "$(code -X POST -H 'Content-Type: application/json' -d '{"action":"triage"}' "$BASE/agent/run")" = 401 ] && ok "anonymous POST /agent/run is refused" || bad "POST /agent/run"
[ "$(code -u "${USER_}:wrong-password" "$BASE/api/cases")" = 401 ] && ok "a wrong password is refused" || bad "wrong password"
[ "$(code -u "${USER_}:${PW}" "$BASE/api/cases")" = 200 ] && ok "the operator is served" || bad "operator GET /api/cases"

$CSCLI cscli decisions delete --ip "$IP" >/dev/null 2>&1 || true
$CSCLI cscli decisions add --ip "$IP" --duration 1h --reason "crowdsecurity/ssh-bf smoke test" >/dev/null
CASE=""; RID=""
i=0
while [ -z "$CASE" ] && [ "$i" -lt 20 ]; do
  out="$(auth "$BASE/api/cases" | json "' '.join((c['case_id']+' '+((c.get('open_action') or {}).get('request_id',''))) for c in d['cases'] if '$IP' in c['title'] and c['status']=='AWAITING_APPROVAL')" 2>/dev/null | head -1 || true)"
  [ -n "$out" ] && { CASE="${out%% *}"; RID="${out##* }"; } || { i=$((i + 1)); sleep 2; }
done
[ -n "$CASE" ] && [ -n "$RID" ] || bad "no case appeared for the injected alert"
ok "the alert for $IP became case $CASE awaiting approval (action $RID)"

[ "$(auth "$BASE/api/cases/$CASE" | json "d['replay']['deterministic']")" = True ] && ok "the case replays deterministically from its stored evidence" || bad "replay"
[ "$(code -X POST -H 'Content-Type: application/json' -d "{\"request_id\":\"$RID\"}" "$BASE/api/cases/$CASE/execute")" = 401 ] && ok "anonymous execute is refused" || bad "anonymous execute"
auth -X POST -H 'Content-Type: application/json' -d "{\"request_id\":\"$RID\",\"approved\":true}" "$BASE/api/cases/$CASE/respond" | json "d['awaiting_execution']" | grep -q True && ok "the operator approved; the decision is recorded under operator:${USER_}" || bad "approve"
EX="$(auth -X POST -H 'Content-Type: application/json' -d "{\"request_id\":\"$RID\"}" "$BASE/api/cases/$CASE/execute")"
echo "$EX" | json "d['receipt']['status']" | grep -Eq 'DRY_RUN|SUCCEEDED' && ok "execution returned a receipt: $(echo "$EX" | json "d['receipt']['status']"), verification: $(echo "$EX" | json "d['verification']")" || bad "execute: $EX"
STATUS_BEFORE="$(auth "$BASE/api/cases" | json "[c['status'] for c in d['cases'] if c['case_id']=='$CASE'][0]")"

eval "$RESTART" >/dev/null
i=0; until [ "$(code "$BASE/readyz")" = 200 ]; do i=$((i + 1)); [ "$i" -gt 30 ] && bad "did not come back after the restart"; sleep 2; done
STATUS_AFTER="$(auth "$BASE/api/cases" | json "[c['status'] for c in d['cases'] if c['case_id']=='$CASE'][0]")"
[ "$STATUS_BEFORE" = "$STATUS_AFTER" ] && ok "after a restart the case is still $STATUS_AFTER and its decision is kept ($(auth "$BASE/api/cases" | json "[c['counts']['decisions'] for c in d['cases'] if c['case_id']=='$CASE'][0]") recorded)" || bad "state changed across restart: $STATUS_BEFORE -> $STATUS_AFTER"

$CSCLI cscli decisions delete --ip "$IP" >/dev/null 2>&1 || true
echo "SMOKE OK"
