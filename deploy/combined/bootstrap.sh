#!/bin/sh
# One-time bootstrap for the combined install (Projects + redevops-qa + Edge Sentinel). Idempotent: it never
# overwrites an existing secret, .env or products.yaml, and the Sentinel step delegates to the standalone
# bootstrap, which is itself idempotent. Nothing secret is printed.
#
#   ./bootstrap.sh            # from this directory; needs Docker, no other prerequisites
#   ./smoke.sh                # afterwards: what this install claims, checked against the running stack
set -eu
cd "$(dirname "$0")"

gen() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
get() { sed -n "s/^$1=//p" .env | tail -1; }
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*" >&2; exit 1; }

umask 077
mkdir -p secrets
[ -f .env ] || cp .env.example .env

PROJ="$(get SENTINEL_PROJECT)"; PROJ="${PROJ:-edge-sentinel}"
NET="$(get SENTINEL_NETWORK)"; NET="${NET:-edge-sentinel_sentinel}"
OPUSER="$(get OPERATOR_USER)"; OPUSER="${OPUSER:-operator}"
NAMED="$(get SENTINEL_NAMED_USER)"; NAMED="${NAMED:-alex}"
PPORT="$(get PROJECTS_PORT)"; PPORT="${PPORT:-8787}"
QPORT="$(get QA_PORT)"; QPORT="${QPORT:-8080}"
SENTINEL_DIR="../standalone/edge-sentinel"

# 1. the Sentinel stack, by its own documented bootstrap. stdin is closed for the whole step: the CrowdSec
#    machine registration reads a template from stdin unless the bootstrap closes it (fixed upstream), and a
#    non-interactive run must never hang on an older checkout.
[ -d "$SENTINEL_DIR" ] || bad "expected the repository checkout: $SENTINEL_DIR holds the published Sentinel stack"
echo "== Edge Sentinel + CrowdSec (standalone bootstrap, project '$PROJ')"
( cd "$SENTINEL_DIR" && COMPOSE_PROJECT_NAME="$PROJ" ./bootstrap.sh < /dev/null >/dev/null ) \
  || bad "the standalone bootstrap failed (run: cd $SENTINEL_DIR && COMPOSE_PROJECT_NAME=$PROJ ./bootstrap.sh)"
SD="$(cd "$SENTINEL_DIR" && pwd)"
SPORT="$(sed -n 's/^EDGE_SENTINEL_PORT=//p' "$SD/.env" 2>/dev/null | tail -1)"; SPORT="${SPORT:-8203}"
[ -s "$SD/secrets/control_token" ] && [ -s "$SD/secrets/approval_secret" ] || bad "missing the standalone secrets (did its bootstrap run?)"
ok "Sentinel stack is up on http://127.0.0.1:${SPORT}/"

# 2. shared credentials: the SAME values on both sides of every hand-off, copied, never re-rolled.
#    (A copied secret is replaced only when the source changed — re-running after re-generating upstream follows along.)
sync_secret() { # name source
  f="secrets/$1"
  if [ ! -s "$f" ] || ! cmp -s "$f" "$2"; then cp "$2" "$f"; fi
}
sync_secret edge-sentinel.service_token "$SD/secrets/control_token"
sync_secret approval_secret "$SD/secrets/approval_secret"
[ -s secrets/operator_basic ] || echo "${OPUSER}:$(gen)" > secrets/operator_basic
[ -s secrets/named_user_password ] || gen > secrets/named_user_password

if [ ! -s secrets/redevops-qa.service_token ]; then gen > secrets/redevops-qa.service_token; fi
[ -s secrets/qa_admin_password ] || gen > secrets/qa_admin_password
QA_TOKEN="$(cat secrets/redevops-qa.service_token)"
APPROVAL_SECRET="$(cat secrets/approval_secret)"
# the two QA values are .env variables (its published compose reads them from there); keep them in step
set_in_env() { # key value
  if grep -q "^$1=" .env; then sed -i.bak "s|^$1=.*|$1=$2|" .env && rm -f .env.bak
  else echo "$1=$2" >> .env; fi
}
set_in_env QA_SERVICE_TOKEN "$QA_TOKEN"
set_in_env QA_APPROVAL_SECRET "$APPROVAL_SECRET"
set_in_env QA_BOOTSTRAP_ADMIN_PASSWORD "$(cat secrets/qa_admin_password)"
ok "shared credentials staged (approval secret identical in Projects, QA and Sentinel)"

# 3. the registry: the supported activation of the product layer. Copy once, then validate it INSIDE the projects
#    image — an operator must not learn about a bad registry from a degraded UI.
[ -f products.yaml ] || cp products.yaml.example products.yaml
PIMG="$(get PROJECTS_IMAGE)"; PIMG="${PIMG:-rails-projects:combined}"
echo "== building the projects image ($PIMG)"
docker compose build projects >/dev/null || bad "the projects image failed to build"
docker run --rm -v "$PWD/products.yaml:/p.yaml:ro" "$PIMG" \
  python3 -c 'from agentic_os.products.registry import load_registry; r = load_registry("/p.yaml"); assert not r.errors, r.errors; print(len(r.products), "products")' >/dev/null 2>&1 \
  || bad "products.yaml does not load in the projects image (see: docker run --rm -v \$PWD/products.yaml:/p.yaml:ro $PIMG python3 -c 'from agentic_os.products.registry import load_registry as l; print(l(\"/p.yaml\").errors)')"
ok "AGENTIC_OS_PRODUCTS registry validated inside the projects image"

# 4. the consumers (QA + Projects) on the Sentinel network.
echo "== starting redevops-qa and Projects"
docker compose --project-directory . up -d --wait --wait-timeout 180 >/dev/null || bad "compose up --wait failed (docker compose ps)"
ok "QA on http://127.0.0.1:${QPORT}/ui/ , Projects on http://127.0.0.1:${PPORT}/"

# 5. identity: a named Sentinel user for the Projects principal to act as (approvals are bound to a person).
SPW="$(cat "$SD/secrets/operator_password")"; SUSER="$(sed -n 's/^SENTINEL_BASIC_AUTH_USER=//p' "$SD/.env" 2>/dev/null | tail -1)"; SUSER="${SUSER:-operator}"
SBASE="http://127.0.0.1:${SPORT}"
api() { curl -sS --max-time 20 -u "${SUSER}:${SPW}" "$@"; }
i=0; until [ "$(curl -s -o /dev/null -w '%{http_code}' "$SBASE/readyz")" = 200 ]; do i=$((i+1)); [ $i -gt 60 ] && bad "Sentinel did not become ready"; sleep 2; done
code="$(api -o /dev/null -w '%{http_code}' -X POST -d "{\"name\":\"${NAMED}\",\"password\":\"$(cat secrets/named_user_password)\",\"role\":\"operator\",\"display_name\":\"Projects operator\"}" "$SBASE/api/v1/users")"
case "$code" in 201|409) ok "Sentinel user '$NAMED' present (role operator)";; *) bad "creating the named user failed (HTTP $code)";; esac
api -o /dev/null -X DELETE "$SBASE/api/v1/actor-links/projects:${OPUSER}" >/dev/null 2>&1 || true
api -X POST -d "{\"external_actor\":\"projects:${OPUSER}\",\"user\":\"${NAMED}\"}" "$SBASE/api/v1/actor-links" >/dev/null
[ "$(api -X GET "$SBASE/api/v1/actor-links" | tr -d ' ' | grep -c "projects:${OPUSER}")" -ge 1 ] \
  || bad "the actor link projects:${OPUSER} -> $NAMED is not visible"
ok "actor link projects:${OPUSER} -> $NAMED"

# 6. health pass through the supported path: Projects must see BOTH products ready, with a matching approval
#    secret on both (a mismatch silently strands every approval as staged).
PB="$(cat secrets/operator_basic)"
papi() { curl -sS --max-time 20 -u "$PB" "http://127.0.0.1:${PPORT}$@"; }
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
echo "Combined install is up. Sign in to Projects at http://127.0.0.1:${PPORT}/ as"
echo "  ${OPUSER} (password: cat secrets/operator_basic after the colon)."
echo "Product UIs:  Edge Sentinel http://127.0.0.1:${SPORT}/ (user ${SUSER}, cat $SD/secrets/operator_password)"
echo "              redevops-qa http://127.0.0.1:${QPORT}/ui/"
echo "Sidekick: open a chat in Projects — the product tools are live once a model endpoint is configured"
echo "(REDEVOPS_LLM_* in the environment; the tools and their gating work without one, chat needs a model)."
echo "Check it all: ./smoke.sh   — tear it down: ./smoke.sh --teardown or see README.md."