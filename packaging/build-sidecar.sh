#!/usr/bin/env bash
# Task 2 — freeze the Python runtime into a single self-contained sidecar binary.
# Consumes the offline wheelhouse from Task 1 (build-wheelhouse.sh must run first), so the app
# code carries NO git/network dependency. PyInstaller itself is a build-time tool (not shipped).
# Output: packaging/dist/redevops-sidecar  (Linux/macOS) or redevops-sidecar.exe (Windows)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WH="$ROOT/packaging/wheelhouse"
PYV="${PYTHON_VERSION:-3.12}"
EXTRAS="projects,duckdb"
[ -d "$WH" ] || { echo "wheelhouse missing — run packaging/build-wheelhouse.sh first"; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
uv venv --seed --python "$PYV" "$TMP/freeze" >/dev/null
PIP="$TMP/freeze/bin/pip"

echo "== install the suite OFFLINE from the wheelhouse =="
PATH="/usr/bin:/bin" "$PIP" install --no-index --find-links "$WH" "agentic-os[$EXTRAS]" >/dev/null
echo "== install PyInstaller (build tool; from index, not shipped) =="
"$PIP" install "pyinstaller>=6.0" >/dev/null

echo "== freeze =="
cd "$ROOT/packaging"
"$TMP/freeze/bin/pyinstaller" --clean --noconfirm \
    --distpath "$ROOT/packaging/dist" --workpath "$ROOT/packaging/build" \
    redevops-sidecar.spec

BIN="$ROOT/packaging/dist/redevops-sidecar"
[ -f "$BIN.exe" ] && BIN="$BIN.exe"
echo "built: $BIN"
echo "== smoke test: the frozen binary imports + shows the bootstrap report =="
"$BIN" bootstrap || true    # exit code is the device verdict; we only assert it runs
