#!/usr/bin/env bash
# Task 2 — freeze the Python runtime into a single self-contained sidecar binary.
# Consumes the offline wheelhouse from Task 1 (build-wheelhouse.sh must run first), so the app
# code carries NO git/network dependency. PyInstaller itself is a build-time tool (not shipped as a
# component, but its bootloader/hooks are inside the executable).
#
# Pinned: CPython $PYTHON_VERSION (exact), pip $PIP_VERSION, PyInstaller $PYINSTALLER_VERSION and its
# dependencies (setuptools, packaging, altgraph, macholib | pefile, pyinstaller-hooks-contrib) from
# locks/build-tools-<os>.lock with --require-hashes; the shipped wheels from locks/requirements-<os>.lock
# with --require-hashes (+ the first-party wheels from the wheelhouse, verified by hash against the
# wheelhouse manifest written by build-wheelhouse.sh).
#
# Output: packaging/dist/redevops-sidecar (Linux/macOS) or redevops-sidecar.exe (Windows),
#         packaging/dist/build-manifest.json (tool versions, input hashes, commit, artifact hash)
set -euo pipefail
. "$(dirname "$0")/common.sh"
T0="$(now)"
WH="$PK/wheelhouse"
EXTRAS="projects,duckdb"
[ -d "$WH" ] || { echo "wheelhouse missing — run packaging/build-wheelhouse.sh first"; exit 1; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "== freeze venv: CPython $PYTHON_VERSION, pip $PIP_VERSION (hash-locked) =="
uv venv --python "$PYTHON_VERSION" "$TMP/freeze" >/dev/null
FPY="$(venv_py "$TMP/freeze")"; FBIN="$(venv_bin "$TMP/freeze")"
assert_python "$FPY"
uv pip install --python "$FPY" --require-hashes --no-deps -r "$LOCKS/pip.lock" >/dev/null
( cd "$WH" && "$FPY" -I - <<'PY'
import hashlib, pathlib, sys
want = dict(l.split("  ", 1)[::-1] for l in pathlib.Path("WHEELS.sha256").read_text().split("\n") if l)
have = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path(".").glob("*.whl")}
if want != have:
    sys.exit("wheelhouse changed since build-wheelhouse.sh wrote WHEELS.sha256: " + str(sorted(set(want.items()) ^ set(have.items()))[:4]))
print("   wheelhouse hashes match WHEELS.sha256 (%d wheels)" % len(have))
PY
)


echo "== install the shipped set OFFLINE: third-party by hash, first-party from the wheelhouse =="
"$FPY" -m pip install --quiet --no-index --find-links "$WH" --require-hashes --no-deps -r "$LOCKS/requirements-$OS.lock"
first_party_wheels "$FPY" "$WH"
# shellcheck disable=SC2086
"$FPY" -m pip install --quiet --no-index --find-links "$WH" --no-deps $FIRST
"$FPY" -m pip check

echo "== install PyInstaller $PYINSTALLER_VERSION + dependencies (build tool; hash-locked, from the index) =="
"$FPY" -m pip install --quiet --require-hashes --no-deps -r "$LOCKS/build-tools-$OS.lock"
"$FBIN/pyinstaller" --version | strip_cr | grep -qx "$PYINSTALLER_VERSION" || { echo "pyinstaller is not $PYINSTALLER_VERSION"; exit 1; }

echo "== freeze =="
# SOURCE_DATE_EPOCH (commit time) and PYTHONHASHSEED are the knobs PyInstaller documents for
# reproducible output; the result is still compared, not assumed (verify-reproducible.sh).
export SOURCE_DATE_EPOCH="$(git -C "$ROOT" show -s --format=%ct HEAD 2>/dev/null || echo 315532800)"
export PYTHONHASHSEED=1
cd "$PK"
"$FBIN/pyinstaller" --clean --noconfirm \
    --distpath "$PK/dist" --workpath "$PK/build" \
    redevops-sidecar.spec

BIN="$PK/dist/redevops-sidecar"
[ -f "$BIN.exe" ] && BIN="$BIN.exe"
echo "built: $BIN"
# Record which Licenses folder (if any) this binary embeds, so build-launcher.sh can refuse a sidecar whose
# embedded notices differ from the folder the launcher bundle will carry (pass 1 embeds none, pass 2 embeds them).
rm -f "$PK/dist/sidecar-licenses.sha256"
if [ -n "${REDEVOPS_LICENSES_DIR:-}" ]; then
  "$FPY" -I -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1]+"/LICENSES-MANIFEST.json","rb").read()).hexdigest())' \
      "$REDEVOPS_LICENSES_DIR" > "$PK/dist/sidecar-licenses.sha256"
  echo "sidecar embeds Licenses (LICENSES-MANIFEST.json sha256 $(cat "$PK/dist/sidecar-licenses.sha256"))"
fi
"$FPY" -m pip freeze --all > "$PK/dist/freeze-venv-pip-freeze.txt"
# the freeze venv is deleted on exit: keep its distribution inventory for the SBOM step (packaging/build-sbom.sh)
"$FPY" -I "$PK/sbom/inventory.py" --dump > "$PK/dist/inventory-env.json"
"$FPY" -I "$PK/tools/build_manifest.py" --root "$ROOT" --out "$PK/dist/build-manifest.json" --stage sidecar --record-python \
    --artifact "sidecar=$BIN" --wheelhouse "$WH" --cmd 'uv="uv --version"' --duration "sidecar=$(( $(now) - T0 ))"
echo "== smoke test: the frozen binary imports + shows the bootstrap report =="
# exit code is the device verdict; we only assert it runs. Scrubbed environment: the report must not touch
# (or print) credentials of the build host.
scrubbed_run "$TMP" "$BIN" bootstrap || true
