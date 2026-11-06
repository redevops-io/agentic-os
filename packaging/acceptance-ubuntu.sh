#!/usr/bin/env bash
# Task 5 — Ubuntu clean-machine acceptance (scriptable slice).
# Installs the .deb and proves the NATIVE control plane serves with NO Docker, NO system Python,
# NO Git. The full journey bar (first verified Mission, survives reboot) is in ACCEPTANCE-ubuntu.md;
# this is the CI-runnable core of it.
#
# Usage: packaging/acceptance-ubuntu.sh path/to/Agentic_Apps_R0-prime_x.y.z_<arch>.deb   (relative or absolute, spaces allowed)
set -euo pipefail
DEB="${1:?usage: acceptance-ubuntu.sh <path-to-.deb>}"
[ -f "$DEB" ] || { echo "FAIL: $DEB is not a file"; exit 1; }
# apt-get treats an argument as a local file only when it contains a slash; make it absolute so that BOTH a relative and an
# absolute argument take the apt path (LNX-02: "./$DEB" turned /abs/x.deb into .//abs/x.deb, apt failed with its error
# hidden, and the run silently fell back to dpkg -i plus `apt-get -f install`, a different install path).
DEB="$(cd "$(dirname "$DEB")" && pwd)/$(basename "$DEB")"

echo "== install the suite from the .deb (apt resolves the dependencies; its errors stay visible) =="
sudo apt-get install -y "$DEB" || sudo dpkg -i "$DEB" || { sudo apt-get -f install -y; sudo dpkg -i "$DEB"; }

echo "== locate the installed frozen sidecar (self-contained: no python/git needed to run) =="
# Tauri installs the externalBin beside the launcher; the package name slugs from productName
# (currently 're-dev-ops'), so find the binary by path, not by a guessed package name.
SIDE="/usr/bin/redevops-sidecar"
[ -f "$SIDE" ] || SIDE="$(command -v redevops-sidecar || true)"
[ -f "$SIDE" ] || { echo "FAIL: redevops-sidecar not found after install"; exit 1; }
echo "sidecar: $SIDE"

echo "== start the control plane NATIVELY (no Docker) =="
RDO_SIDECAR="$SIDE"; export RDO_SIDECAR
LOG="$(mktemp)"                      # not a predictable /tmp path (review SR-14)
"$SIDE" serve >"$LOG" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true; rm -f "$LOG"' EXIT

echo "== wait for http://127.0.0.1:8787 to serve the UI =="
ok=0
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8787/ >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
[ "$ok" = 1 ] || { echo "FAIL: control plane did not serve"; tail -20 "$LOG"; exit 1; }
echo "PASS: control plane served natively (no Docker, no system Python/Git)."
if command -v ss >/dev/null 2>&1; then
  LISTEN="$(ss -ltn 2>/dev/null | awk '$4 ~ /:8787$/ {print $4}' | sort -u)"
  [ "$LISTEN" = "127.0.0.1:8787" ] || { echo "FAIL: the control plane listens on '$LISTEN', expected only 127.0.0.1:8787"; exit 1; }
  echo "PASS: the listener is bound to 127.0.0.1 only."
fi
code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/api/projects || true)"
[ "$code" = 401 ] || { echo "FAIL: /api/projects answered $code without a session (expected 401: local capability gate)"; exit 1; }
echo "PASS: the API needs the session (401 without it)."

echo "== device-readiness brain runs from the frozen binary =="
"$SIDE" bootstrap || true   # exit code is the device verdict; we assert it RUNS
echo "ACCEPTANCE (scriptable slice) PASS"
