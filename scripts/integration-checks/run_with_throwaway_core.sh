#!/usr/bin/env bash
# Start a THROWAWAY instance of one product in Docker, run the matching integration check against it, and remove everything again.
#   usage: scripts/integration-checks/run_with_throwaway_core.sh chatwoot|lago|twenty
# Needs Docker, curl, openssl and a Python that can import the apps (PYTHONPATH=.:apps from the repository root, app requirements installed).
# Nothing outside the containers, the network and the temporary directory it creates is touched; every credential is generated here, used
# once and never printed. The image tags are the versions the checks were last run against; override with CORE_IMAGE to try another.
set -euo pipefail
core="${1:-}"
case "$core" in chatwoot|lago|twenty) ;; *) echo "usage: $0 chatwoot|lago|twenty" >&2; exit 2;; esac
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
py="${PYTHON:-python3}"
p="ic-${core}-$$"; net="$p-net"; work="$(mktemp -d)"
rnd() { openssl rand -hex "${1:-16}"; }
cleanup() { # only what this run created: everything carries the label ic.run=<this run>
  c="$(docker ps -aq --filter "label=ic.run=$p" 2>/dev/null || true)"
  [ -z "$c" ] || docker rm -f -v $c >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
  rm -rf -- "$work"
}
trap cleanup EXIT
run() { docker run -d --label "ic.run=$p" --network "$net" "$@" >/dev/null; }
wait_http() { # url, seconds
  for _ in $(seq 1 "$(( $2 / 3 ))"); do [ "$(curl -s -o /dev/null -m 4 -w '%{http_code}' "$1" || true)" = 200 ] && return 0; sleep 3; done
  echo "the product did not answer 200 at $1 within $2 s" >&2; return 1
}
free_port() { "$py" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }
docker network create --label "ic.run=$p" "$net" >/dev/null
port="$(free_port)"; pgpw="$(rnd)"
export PYTHONPATH="$root:$root/apps" AGENTIC_OS_PROFILE=development

if [ "$core" = chatwoot ]; then
  image="${CORE_IMAGE:-chatwoot/chatwoot:v4.18.0}"
  cw=(-e SECRET_KEY_BASE="$(rnd 32)" -e FRONTEND_URL="http://127.0.0.1:$port" -e RAILS_ENV=production -e NODE_ENV=production -e INSTALLATION_ENV=docker
      -e POSTGRES_HOST="$p-db" -e POSTGRES_USERNAME=postgres -e POSTGRES_PASSWORD="$pgpw" -e POSTGRES_DATABASE=chatwoot -e REDIS_URL="redis://$p-redis:6379"
      -e ENABLE_ACCOUNT_SIGNUP=false -e FORCE_SSL=false -e RAILS_LOG_TO_STDOUT=true)
  run --name "$p-db" -e POSTGRES_PASSWORD="$pgpw" -e POSTGRES_DB=chatwoot pgvector/pgvector:pg16
  run --name "$p-redis" redis:7-alpine
  sleep 8
  docker run --rm --label "ic.run=$p" --network "$net" "${cw[@]}" "$image" bundle exec rails db:chatwoot_prepare >"$work/prepare.log" 2>&1 || { tail -5 "$work/prepare.log" >&2; exit 1; }
  run --name "$p-rails" -p "127.0.0.1:$port:3000" "${cw[@]}" "$image" bundle exec rails s -p 3000 -b 0.0.0.0
  run --name "$p-sidekiq" "${cw[@]}" "$image" bundle exec sidekiq -C config/sidekiq.yml
  wait_http "http://127.0.0.1:$port/api" 240
  upw="Aa1!$(rnd 10)"
  token="$(docker exec -e IC_PW="$upw" "$p-rails" bundle exec rails runner '
    a = Account.create!(name: "Integration Check")
    u = User.new(name: "Check Operator", email: "operator@example.com", password: ENV["IC_PW"], password_confirmation: ENV["IC_PW"]); u.skip_confirmation!; u.save!
    AccountUser.create!(account: a, user: u, role: :administrator)
    print u.access_token.token' 2>/dev/null | tail -1)"
  [ -n "$token" ] || { echo "no API token could be created in the throwaway Chatwoot" >&2; exit 1; }
  account="$(docker exec "$p-rails" bundle exec rails runner 'print Account.last.id' 2>/dev/null | tail -1)"
  CHATWOOT_API_URL="http://127.0.0.1:$port" CHATWOOT_ACCOUNT_ID="$account" CHATWOOT_API_TOKEN="$token" OPERATOR_BASIC_AUTH="operator:$(rnd)" \
    "$py" "$here/check_support_chatwoot.py"

elif [ "$core" = lago ]; then
  image="${CORE_IMAGE:-getlago/api:v1.53.0}"
  key="$(rnd 24)"; rsa="$(openssl genrsa 2048 2>/dev/null | base64 | tr -d '\n')"
  lg=(-e DATABASE_URL="postgresql://lago:$pgpw@$p-db:5432/lago?search_path=public" -e REDIS_URL="redis://$p-redis:6379" -e RAILS_ENV=production -e RAILS_LOG_TO_STDOUT=true
      -e SECRET_KEY_BASE="$(rnd 32)" -e LAGO_RSA_PRIVATE_KEY="$rsa" -e LAGO_ENCRYPTION_PRIMARY_KEY="$(rnd 16)" -e LAGO_ENCRYPTION_DETERMINISTIC_KEY="$(rnd 16)"
      -e LAGO_ENCRYPTION_KEY_DERIVATION_SALT="$(rnd 16)" -e LAGO_API_URL="http://127.0.0.1:$port" -e LAGO_FRONT_URL="http://127.0.0.1:$port" -e LAGO_DISABLE_SIGNUP=true
      -e LAGO_DISABLE_SEGMENT=true -e LAGO_DISABLE_PDF_GENERATION=true -e LAGO_USE_AWS_S3=false -e LAGO_USE_GCS=false)
  run --name "$p-db" -e POSTGRES_USER=lago -e POSTGRES_PASSWORD="$pgpw" -e POSTGRES_DB=lago getlago/postgres-partman:15.0-alpine
  run --name "$p-redis" redis:7-alpine
  sleep 10
  docker run --rm --label "ic.run=$p" --network "$net" "${lg[@]}" "$image" ./scripts/migrate.sh >"$work/migrate.log" 2>&1 || { tail -5 "$work/migrate.log" >&2; exit 1; }
  run --name "$p-api" -p "127.0.0.1:$port:3000" "${lg[@]}" "$image" ./scripts/start.api.sh
  run --name "$p-worker" "${lg[@]}" "$image" ./scripts/start.worker.sh
  wait_http "http://127.0.0.1:$port/health" 240
  docker exec -e LAGO_CREATE_ORG=true -e LAGO_ORG_USER_EMAIL=check@example.com -e LAGO_ORG_USER_PASSWORD="Aa1-$(rnd 8)" -e LAGO_ORG_NAME="Integration Check" -e LAGO_ORG_API_KEY="$key" \
    "$p-api" bundle exec rake signup:seed_organization >"$work/seed.log" 2>&1 || { tail -5 "$work/seed.log" >&2; exit 1; }
  export LAGO_API_URL="http://127.0.0.1:$port" LAGO_API_KEY="$key" BILLING_VERIFY_ATTEMPTS=3 BILLING_VERIFY_INTERVAL_S=2 CORE_VERSION="${image##*:}"
  "$py" "$here/check_billing_lago.py" setup | tail -1 > "$work/ids.json"
  # time cannot be advanced through the API: the two invoices just created are marked overdue directly, as the clock job would after their due date
  docker exec -i "$p-api" bundle exec rails runner 'ids = JSON.parse(STDIN.read).values.map { |v| v["lago_id"] }; Invoice.where(id: ids).update_all(payment_overdue: true, payment_due_date: 3.days.ago.to_date)' < "$work/ids.json" >/dev/null 2>&1
  "$py" "$here/check_billing_lago.py" run "$(cat "$work/ids.json")"

else
  image="${CORE_IMAGE:-twentycrm/twenty:v2.27.0}"
  run --name "$p-db" -e POSTGRES_PASSWORD="$pgpw" -e POSTGRES_DB=default pgvector/pgvector:pg16
  run --name "$p-redis" redis:7-alpine
  sleep 8
  run --name "$p-server" -p "127.0.0.1:$port:3000" -e PG_DATABASE_URL="postgres://postgres:$pgpw@$p-db:5432/default" -e REDIS_URL="redis://$p-redis:6379" \
      -e APP_SECRET="$(rnd 32)" -e SERVER_URL="http://127.0.0.1:$port" -e SIGN_IN_PREFILLED=true -e STORAGE_TYPE=local "$image"
  wait_http "http://127.0.0.1:$port/healthz" 420
  # Twenty's own development workspace (its sample company and its prefilled sample user): a fresh instance has no workspace to create a key in
  docker exec "$p-server" sh -c 'cd /app/packages/twenty-server 2>/dev/null || cd /app; node dist/command/command workspace:seed:dev --light' >"$work/seed.log" 2>&1 || { tail -5 "$work/seed.log" >&2; exit 1; }
  TWENTY_BASE="http://127.0.0.1:$port" "$py" "$here/twenty_api_key.py" > "$work/key"
  TWENTY_URL="http://127.0.0.1:$port" TWENTY_PUBLIC_URL="http://127.0.0.1:$port" TWENTY_API_KEY="$(cat "$work/key")" CORE_VERSION="${image##*:}" \
    "$py" "$here/check_outreach_twenty.py"
fi
