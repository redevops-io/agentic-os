#!/bin/sh
# Stop ONLY a gateway this harness started: the process listening on PORT, and, when WORKDIR is given, only if its pid is the one start_gateway.sh recorded in
# WORKDIR/gateway.pid. Never matches by command name: other sessions and other harness runs start the same module, and a name match is not ownership.
# usage: stop_gateway.sh PORT [WORKDIR]
set -eu
PORT="$1"; W="${2:-}"
pid="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
[ -z "$pid" ] && exit 0                                                     # nothing listens there: nothing to stop
if [ -n "$W" ]; then
  [ -f "$W/gateway.pid" ] || { echo "refusing: $W/gateway.pid does not exist, so $pid on port $PORT is not known to be ours" >&2; exit 3; }
  [ "$(cat "$W/gateway.pid")" = "$pid" ] || { echo "refusing: port $PORT is held by $pid, not by the pid this harness recorded ($(cat "$W/gateway.pid"))" >&2; exit 3; }
fi
kill "$pid" 2>/dev/null || true
for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || { rm -f "${W:+$W/gateway.pid}"; exit 0; }; sleep 1; done
echo "pid $pid on port $PORT did not exit" >&2; exit 1
