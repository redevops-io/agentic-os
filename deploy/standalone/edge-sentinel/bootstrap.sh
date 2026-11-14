#!/bin/sh
# One-time bootstrap for the Docker Compose deployment. Idempotent: it never overwrites an existing secret.
#   1. creates ./secrets/{operator_password,crowdsec_bouncer_key,crowdsec_machine_password,control_token,approval_secret} (random, mode 0600)
#   2. creates .env from .env.example when there is none
#   3. starts CrowdSec, registers the bouncer key and the watcher login Sentinel uses, then starts Sentinel
# Nothing secret is printed: the operator password is the file ./secrets/operator_password.
set -eu
cd "$(dirname "$0")"

gen() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
umask 077
mkdir -p secrets
created=""
for name in operator_password crowdsec_bouncer_key crowdsec_machine_password control_token approval_secret; do
  if [ ! -s "secrets/$name" ]; then gen > "secrets/$name"; created="$created $name"; fi
done
[ -f .env ] || cp .env.example .env

COMPOSE="docker compose"
$COMPOSE up -d crowdsec
echo "waiting for CrowdSec to be healthy ..."
i=0
until [ "$($COMPOSE ps crowdsec --format '{{.Health}}')" = "healthy" ]; do
  i=$((i + 1)); [ "$i" -gt 60 ] && { echo "CrowdSec did not become healthy" >&2; exit 1; }
  sleep 2
done

# the credentials Sentinel reads from /run/secrets; re-running replaces them with the file contents
$COMPOSE exec -T crowdsec cscli bouncers delete edge-sentinel >/dev/null 2>&1 || true
$COMPOSE exec -T crowdsec cscli bouncers add edge-sentinel --key "$(cat secrets/crowdsec_bouncer_key)" >/dev/null 2>&1 \
  || { echo "registering the bouncer key failed (run: $COMPOSE logs crowdsec)" >&2; exit 1; }
# cscli echoes the credentials it registers on stderr; neither stream may reach a terminal or a CI log
$COMPOSE exec -T crowdsec cscli machines add edge-sentinel --password "$(cat secrets/crowdsec_machine_password)" -f - --force >/dev/null 2>&1 \
  || { echo "registering the watcher login failed (run: $COMPOSE logs crowdsec)" >&2; exit 1; }

$COMPOSE up -d
user="$(sed -n 's/^SENTINEL_BASIC_AUTH_USER=//p' .env | tail -1)"; user="${user:-operator}"
port="$(sed -n 's/^EDGE_SENTINEL_PORT=//p' .env | tail -1)"; port="${port:-8203}"
echo
echo "Edge Sentinel is starting on http://127.0.0.1:${port}/ (sign in as '${user}')."
echo "The operator password is in ./secrets/operator_password (read it with: cat secrets/operator_password)."
[ -z "$created" ] || echo "Created secrets:$created"
