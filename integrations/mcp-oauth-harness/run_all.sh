#!/bin/sh
# usage: run_all.sh CHECKOUT [PORT]    Runs the isolation test, then every scenario against a throwaway gateway built from CHECKOUT. Exit 1 on any unexpected result.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"; WT="$(cd "$1" && pwd)"; PORT="${2:-28300}"
W="$(mktemp -d)"; trap '"$here/stop_gateway.sh" "$PORT" "$W" || true' EXIT
sh "$here/isolation_test.sh"
"$here/start_gateway.sh" "$WT" "$W" "$PORT"
cd "$here"
ISSUER="http://localhost:$PORT" WORK="$W" WT="$WT" node --no-warnings scenarios.mjs
cp "$W/results.json" "${RESULTS_OUT:-$here/last-results.json}"
