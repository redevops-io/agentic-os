#!/bin/sh
# One-time bootstrap for the SINGLE-PULL bundle (ghcr.io/redevops-io/redevops-ts): Projects + Sidekick,
# redevops-qa and Edge Sentinel + CrowdSec, pulled as one OCI artifact — no git checkout of this repository
# needed, only this one script and compose.bundle.yml (or the artifact reference alone, with this script's
# logic reproduced by hand; see BUNDLE.md).
#
#   ./bundle-bootstrap.sh            # idempotent: generates every credential once, pulls, starts, wires, verifies
#   docker compose -f oci://ghcr.io/redevops-io/redevops-ts:<tag> --project-directory . --env-file .env.bundle ps
#
# Nothing secret is printed. Every credential lives in .env.bundle (mode 0600) as a plain value — the bundle
# has no file-based secret, so there is nothing to chmod for a container uid the way the file-secrets install does.
set -eu
cd "$(dirname "$0")"

gen() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
get() { sed -n "s/^$1=//p" .env.bundle | tail -1; }
set_in_env() { # key value
  if grep -q "^$1=" .env.bundle; then sed -i.bak "s|^$1=.*|$1=$2|" .env.bundle && rm -f .env.bundle.bak
  else echo "$1=$2" >> .env.bundle; fi
}
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*" >&2; exit 1; }

umask 077
[ -f .env.bundle ] || cp .env.bundle.example .env.bundle

BUNDLE_REF="ghcr.io/redevops-io/redevops-ts:$(get BUNDLE_TAG || true)"; BUNDLE_REF="${BUNDLE_REF%:}"
[ "$BUNDLE_REF" = "ghcr.io/redevops-io/redevops-ts:" ] && BUNDLE_REF="ghcr.io/redevops-io/redevops-ts:0.1.6"
COMPOSE="docker compose -f oci://${BUNDLE_REF} --project-directory . --env-file .env.bundle"
PPORT="$(get PROJECTS_PORT)"; PPORT="${PPORT:-8787}"
QPORT="$(get QA_PORT)"; QPORT="${QPORT:-8080}"
SPORT="$(get EDGE_SENTINEL_PORT)"; SPORT="${SPORT:-8203}"
OPUSER="$(get OPERATOR_USER)"; OPUSER="${OPUSER:-operator}"
NAMED="$(get SENTINEL_NAMED_USER)"; NAMED="${NAMED:-alex}"

# 1. every credential, generated once, as a plain value in .env.bundle (never a file: this artifact has none).
[ -n "$(get OPERATOR_BASIC_AUTH)" ] || set_in_env OPERATOR_BASIC_AUTH "${OPUSER}:$(gen)"
[ -n "$(get SENTINEL_BASIC_AUTH_PASS)" ] || set_in_env SENTINEL_BASIC_AUTH_PASS "$(gen)"
[ -n "$(get CROWDSEC_BOUNCER_KEY)" ] || set_in_env CROWDSEC_BOUNCER_KEY "$(gen)"
[ -n "$(get CROWDSEC_MACHINE_PASSWORD)" ] || set_in_env CROWDSEC_MACHINE_PASSWORD "$(gen)"
[ -n "$(get EDGE_SENTINEL_SERVICE_TOKEN)" ] || set_in_env EDGE_SENTINEL_SERVICE_TOKEN "$(gen)"
[ -n "$(get QA_SERVICE_TOKEN)" ] || set_in_env QA_SERVICE_TOKEN "$(gen)"
[ -n "$(get QA_BOOTSTRAP_ADMIN_PASSWORD)" ] || set_in_env QA_BOOTSTRAP_ADMIN_PASSWORD "$(gen)"
[ -n "$(get APPROVAL_SECRET)" ] || set_in_env APPROVAL_SECRET "$(gen)"   # shared by Projects, QA and Sentinel
ok "credentials present in .env.bundle"

# 1b. redevops-qa's test lanes exist for linux/amd64 only. Where this host cannot execute amd64 images (arm64 Linux without QEMU user
#     emulation) the pinned amd64 image dies with "exec format error", so run the native image there and say what that costs. Docker Desktop
#     on Apple Silicon and an arm64 host with binfmt/QEMU emulate amd64 and keep the lanes.
if [ -z "$(get QA_PLATFORM)" ]; then
  case "$(uname -m)" in
    x86_64|amd64) : ;;
    *) if docker run --rm --platform linux/amd64 busybox:1.37 true >/dev/null 2>&1; then
         ok "this $(uname -m) host can run linux/amd64 images (emulation): redevops-qa keeps its test lanes"
       else
         set_in_env QA_PLATFORM "linux/$(uname -m | sed 's/aarch64/arm64/')"
         echo "NOTE  this host cannot run linux/amd64 images: redevops-qa runs natively ($(get QA_PLATFORM)) and its test lanes are UNAVAILABLE here"
         echo "      (ARCHITECTURE_UNSUPPORTED). To get them: docker run --privileged --rm tonistiigi/binfmt --install amd64, remove QA_PLATFORM from .env.bundle, re-run."
       fi ;;
  esac
fi

# 2. pull the bundle and bring every service up. -y: an oci:// stack's `up` otherwise prompts to confirm every
#    interpolated ${VAR} before proceeding, which hangs forever (reads EOF, "operation cancelled by user") with
#    no stdin attached — observed 2026-11-15 publishing ghcr.io/redevops-io/redevops-ts:0.1.0.
echo "== pulling and starting ${BUNDLE_REF}"
$COMPOSE up -d -y --wait --wait-timeout 60 crowdsec >/dev/null || bad "crowdsec did not become healthy ($COMPOSE logs crowdsec)"

# 3. register CrowdSec's bouncer key and watcher login — same two commands and the same stdin-closing fix as
#    deploy/standalone/edge-sentinel/bootstrap.sh (an open stdin here hangs forever: observed 2026-11-14).
$COMPOSE exec -T crowdsec cscli bouncers delete edge-sentinel >/dev/null 2>&1 || true
$COMPOSE exec -T crowdsec cscli bouncers add edge-sentinel --key "$(get CROWDSEC_BOUNCER_KEY)" >/dev/null 2>&1 \
  || bad "registering the bouncer key failed ($COMPOSE logs crowdsec)"
$COMPOSE exec -T crowdsec cscli machines add edge-sentinel --password "$(get CROWDSEC_MACHINE_PASSWORD)" -f - --force < /dev/null >/dev/null 2>&1 \
  || bad "registering the watcher login failed ($COMPOSE logs crowdsec)"
ok "CrowdSec credentials registered"

$COMPOSE up -d -y --wait --wait-timeout 180 >/dev/null || bad "compose up --wait failed ($COMPOSE ps)"
ok "Edge Sentinel http://127.0.0.1:${SPORT}/ , redevops-qa http://127.0.0.1:${QPORT}/ui/ , Projects http://127.0.0.1:${PPORT}/"

# 4. identity: link a named Sentinel user to the Projects principal (approvals are bound to a person, not a service call).
SUSER=operator
SPW="$(get SENTINEL_BASIC_AUTH_PASS)"
api() { curl -sS --max-time 20 -u "${SUSER}:${SPW}" "$@"; }
i=0; until [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${SPORT}/readyz")" = 200 ]; do
  i=$((i+1)); [ $i -gt 60 ] && bad "Sentinel did not become ready"; sleep 2
done
code="$(api -o /dev/null -w '%{http_code}' -X POST \
  -d "{\"name\":\"${NAMED}\",\"password\":\"$(gen)\",\"role\":\"operator\",\"display_name\":\"Projects operator\"}" \
  "http://127.0.0.1:${SPORT}/api/v1/users")"
case "$code" in 201|409) ok "Sentinel user '$NAMED' present (role operator)";; *) bad "creating the named user failed (HTTP $code)";; esac
api -o /dev/null -X DELETE "http://127.0.0.1:${SPORT}/api/v1/actor-links/projects:${OPUSER}" >/dev/null 2>&1 || true
api -X POST -d "{\"external_actor\":\"projects:${OPUSER}\",\"user\":\"${NAMED}\"}" "http://127.0.0.1:${SPORT}/api/v1/actor-links" >/dev/null
ok "actor link projects:${OPUSER} -> $NAMED"

# 5. health pass through the supported path: Projects must see BOTH products ready, approval secret matching.
PB="$(get OPERATOR_BASIC_AUTH)"
papi() { curl -sS --max-time 20 -u "$PB" "http://127.0.0.1:${PPORT}$1"; }
i=0
while :; do
  out="$(papi /api/products 2>/dev/null || true)"
  ready="$(echo "$out" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    want={"edge-sentinel","redevops-qa"}
    got={p["id"] for p in d["products"] if p.get("ready") and p.get("approval_secret") and p.get("capabilities")}
    print("yes" if want <= got else "no")
except Exception: print("no")' 2>/dev/null || echo no)"
  [ "$ready" = yes ] && break
  i=$((i+1)); [ $i -gt 45 ] && { echo "--- last /api/products ---"; echo "$out"; bad "products did not all become ready"; }
  sleep 2
done
ok "Projects sees edge-sentinel and redevops-qa ready, capabilities advertised, approval secret set"

echo
echo "Bundle is up. Sign in to Projects at http://127.0.0.1:${PPORT}/ as ${OPUSER} (see .env.bundle, OPERATOR_BASIC_AUTH)."
echo "Edge Sentinel: http://127.0.0.1:${SPORT}/ (user ${SUSER}, see .env.bundle, SENTINEL_BASIC_AUTH_PASS)."
echo "redevops-qa:   http://127.0.0.1:${QPORT}/ui/"
echo "Teardown: docker compose -f oci://${BUNDLE_REF} --project-directory . --env-file .env.bundle down -v"
