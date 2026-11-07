#!/usr/bin/env bash
# Freeze packaging/probes/trusted_import_probe.py with the sidecar's spec (same excludes and hidden imports) into
# packaging/dist/redevops-trusted-import-probe, so trusted_import can be exercised inside the PyInstaller runtime (D-NEW-3).
# Same inputs and venv recipe as packaging/build-sidecar.sh: the offline wheelhouse (build-wheelhouse.sh first), the hash locks, the
# pinned PyInstaller. A TEST artefact: never staged, never shipped, not in the SBOM.
#
#   packaging/probes/build-trusted-import-probe.sh && \
#   REDEVOPS_FROZEN_TRUSTED_IMPORT_PROBE=packaging/dist/redevops-trusted-import-probe pytest tests/test_frozen_trusted_import.py
set -euo pipefail
. "$(dirname "$0")/../common.sh"
WH="$PK/wheelhouse"
[ -d "$WH" ] || { echo "wheelhouse missing - run packaging/build-wheelhouse.sh first"; exit 1; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
uv venv --python "$PYTHON_VERSION" "$TMP/freeze" >/dev/null
FPY="$(venv_py "$TMP/freeze")"; FBIN="$(venv_bin "$TMP/freeze")"
assert_python "$FPY"
uv pip install --python "$FPY" --require-hashes --no-deps -r "$LOCKS/pip.lock" >/dev/null
"$FPY" -m pip install --quiet --no-index --find-links "$WH" --require-hashes --no-deps -r "$LOCKS/requirements-$OS.lock"
first_party_wheels "$FPY" "$WH"
# shellcheck disable=SC2086
"$FPY" -m pip install --quiet --no-index --find-links "$WH" --no-deps $FIRST
"$FPY" -m pip install --quiet --require-hashes --no-deps -r "$LOCKS/build-tools-$OS.lock"
export SOURCE_DATE_EPOCH="$(git -C "$ROOT" show -s --format=%ct HEAD 2>/dev/null || echo 315532800)"
export PYTHONHASHSEED=1
cd "$PK"
"$FBIN/pyinstaller" --clean --noconfirm --distpath "$PK/dist" --workpath "$PK/build/probe" probes/trusted-import-probe.spec
BIN="$PK/dist/redevops-trusted-import-probe"; [ -f "$BIN.exe" ] && BIN="$BIN.exe"
[ -f "$BIN" ] || { echo "probe not built"; exit 1; }
echo "probe: $BIN"
