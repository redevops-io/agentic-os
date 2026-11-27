#!/bin/sh
# Stopping one harness must not terminate another process, even one started with the IDENTICAL command line, and the scripts must not match by name.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
A=$((31000 + $$ % 400)); B=$((A + 1)); C=$((A + 2))
d1="$(mktemp -d)"; d2="$(mktemp -d)"
cleanup() { for p in "$A" "$B"; do for pid in $(lsof -ti "tcp:$p" -sTCP:LISTEN 2>/dev/null); do kill "$pid" 2>/dev/null || true; done; done; rm -rf "$d1" "$d2"; }
trap cleanup EXIT
ok() { echo "PASS $1"; }; bad() { echo "FAIL $1" >&2; exit 1; }
(cd "$d1" && exec python3 -m http.server "$A" --bind 127.0.0.1 >/dev/null 2>&1 &)      # two servers with the same command line (only the port differs)
(cd "$d2" && exec python3 -m http.server "$B" --bind 127.0.0.1 >/dev/null 2>&1 &)
for p in "$A" "$B"; do for i in $(seq 1 30); do lsof -ti "tcp:$p" -sTCP:LISTEN >/dev/null 2>&1 && break; sleep 0.3; done; done
echo "$(lsof -ti "tcp:$A" -sTCP:LISTEN | head -1)" > "$d1/gateway.pid"
echo "$(lsof -ti "tcp:$B" -sTCP:LISTEN | head -1)" > "$d2/gateway.pid"
[ -s "$d1/gateway.pid" ] && [ -s "$d2/gateway.pid" ] && ok "two servers with identical command lines are running (pids $(cat "$d1/gateway.pid") and $(cat "$d2/gateway.pid"))" || bad "could not start the two servers"
"$here/stop_gateway.sh" "$A" "$d1"
lsof -ti "tcp:$A" -sTCP:LISTEN >/dev/null 2>&1 && bad "the first server is still running" || ok "stopping the first stops it"
curl -s -o /dev/null -m5 "http://127.0.0.1:$B/" && ok "the second server, same command line, is untouched" || bad "the second server was terminated"
# a stop for a port held by a process that is NOT the recorded pid is refused, and signals nothing
echo 1 > "$d2/gateway.pid"
if "$here/stop_gateway.sh" "$B" "$d2" 2>/dev/null; then bad "stopped a process whose pid was not the recorded one"; else ok "a stop whose pid does not match the recorded pid is refused"; fi
curl -s -o /dev/null -m5 "http://127.0.0.1:$B/" && ok "and that process is still running" || bad "the refused stop still terminated it"
# a port nobody listens on is a no-op
"$here/stop_gateway.sh" "$C" && ok "stopping a port with no listener is a no-op" || bad "stopping an empty port failed"
# static: no script of the harness kills by name
if grep -nE 'pkill|killall|pgrep -f|kill -9 -1' "$here/start_gateway.sh" "$here/stop_gateway.sh" "$here/run_all.sh" "$here/scenarios.mjs" "$here/lib.mjs"; then bad "a harness script matches processes by name"; else ok "no harness script kills by name"; fi
echo "ISOLATION OK"
