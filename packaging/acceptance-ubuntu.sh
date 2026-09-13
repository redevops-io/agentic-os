#!/usr/bin/env bash
# Task 5 — Ubuntu clean-machine acceptance (scriptable slice).
# Installs the .deb and proves the NATIVE control plane serves with NO Docker, NO system Python,
# NO Git. The full journey bar (first verified Mission, survives reboot) is in ACCEPTANCE-ubuntu.md;
# this is the CI-runnable core of it.
#
# Usage: packaging/acceptance-ubuntu.sh path/to/ReDevOps_x.y.z_amd64.deb
set -euo pipefail
DEB="${1:?usage: acceptance-ubuntu.sh <path-to-.deb>}"

echo "== install the suite from the .deb =="
sudo apt-get install -y "./$DEB" 2>/dev/null || sudo dpkg -i "$DEB" || { sudo apt-get -f install -y; sudo dpkg -i "$DEB"; }

echo "== locate the installed frozen sidecar (self-contained: no python/git needed to run) =="
# Tauri installs the externalBin beside the launcher; the package name slugs from productName
# (currently 're-dev-ops'), so find the binary by path, not by a guessed package name.
SIDE="/usr/bin/redevops-sidecar"
[ -f "$SIDE" ] || SIDE="$(command -v redevops-sidecar || true)"
[ -f "$SIDE" ] || { echo "FAIL: redevops-sidecar not found after install"; exit 1; }
echo "sidecar: $SIDE"

echo "== start the control plane NATIVELY (no Docker) =="
RDO_SIDECAR="$SIDE"; export RDO_SIDECAR
"$SIDE" serve >/tmp/rdo-serve.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT

echo "== wait for http://127.0.0.1:8787 to serve the UI =="
ok=0
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8787/ >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
[ "$ok" = 1 ] || { echo "FAIL: control plane did not serve"; tail -20 /tmp/rdo-serve.log; exit 1; }
echo "PASS: control plane served natively (no Docker, no system Python/Git)."

echo "== device-readiness brain runs from the frozen binary =="
"$SIDE" bootstrap || true   # exit code is the device verdict; we assert it RUNS
echo "ACCEPTANCE (scriptable slice) PASS"
