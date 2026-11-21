#!/usr/bin/env bash
# Task 1 — the reproducible wheelhouse: every shipped Python component as a wheel, from PINNED inputs.
#
#   third-party       pip download --only-binary --require-hashes -r locks/requirements-<os>.lock (21 wheels)
#   first-party       built from CLEAN, verified clones of the pinned tags (every entry of
#                     packaging/git-pins.json) and from the tracked files of this checkout (agentic-os),
#                     with backends installed from
#                     locks/build-backends-<os>.lock and `--no-build-isolation`
#
# No unpinned build input remains: the interpreter must be exactly CPython $PYTHON_VERSION, pip/
# setuptools/wheel/hatchling come from a hash lock, the clones are checked against tag object AND
# commit, the committed stale build/ of runtime-contracts v0.3.4 is removed, and every built .py is
# compared with `git show <commit>:<path>`.
#
# windows-arm64 carve-out: PyPI stopped publishing win_arm64 wheels for cryptography after 46.0.3
# (checked 2026-11-21; the pin here, 50.0.1, has none). Every OTHER third-party pin in
# requirements-windows.lock still ships a win_arm64 wheel, so only cryptography is special-cased: the
# other 20 are pulled exactly as before, and cryptography is built from its PyPI sdist (hash below is
# PyPI's own published sha256 for the tarball, checked independently of requirements-windows.lock -
# that file's hash is the win_amd64 WHEEL's hash and can never match a from-source build's bytes)
# against vcpkg's openssl:arm64-windows port, statically linked so the shipped wheel has no runtime
# OpenSSL dependency, matching the prebuilt wheel's behavior. Provisioning vcpkg itself (OPENSSL_DIR /
# OPENSSL_STATIC) is the calling WORKFLOW's job, not this script's - same split already used for
# Python/Rust/uv, which are also installed by workflow steps, not by this script.
#
# Output: packaging/wheelhouse/*.whl, WHEELS.sha256, wheelhouse-manifest.json (+ packaging/dist/build-manifest.json)
# Runs on Linux, macOS (bash 3.2) and Windows git-bash. Needs uv and git; network for PyPI and GitHub.
set -euo pipefail
. "$(dirname "$0")/common.sh"
T0="$(now)"
WH="$PK/wheelhouse"
EXTRAS="projects,duckdb"   # the suite the native installer ships (rag/postgres/mcp excluded)

rm -rf "$WH"; mkdir -p "$WH" "$PK/dist"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "== build venv: CPython $PYTHON_VERSION (unseeded) + build backends from the hash lock =="
uv venv --python "$PYTHON_VERSION" "$TMP/build" >/dev/null
BPY="$(venv_py "$TMP/build")"
assert_python "$BPY"
uv pip install --python "$BPY" --require-hashes --no-deps -r "$LOCKS/build-backends-$OS.lock" >/dev/null
echo "   $("$BPY" -m pip --version | cut -d' ' -f1-2), setuptools $("$BPY" -c 'import setuptools;print(setuptools.__version__)' | strip_cr)"
[ "$("$BPY" -m pip --version | cut -d' ' -f2)" = "$PIP_VERSION" ] || { echo "pip is not the pinned $PIP_VERSION"; exit 1; }
echo "== pins check (pyproject, git-pins.json, requirements.in, locks, Cargo.lock) =="
"$BPY" -I "$PK/verify-pins.py" --quiet || { echo "pins are inconsistent: run packaging/verify-pins.py"; exit 1; }

echo "== 1/4 third-party wheels: pip download --require-hashes (binary only, no build) =="
EFFECTIVE_LOCK="$LOCKS/requirements-$OS.lock"
if [ "$OS" = windows ] && [ "$ARCH" = arm64 ]; then
  [ -n "${OPENSSL_DIR:-}" ] || { echo "ERROR: OPENSSL_DIR not set (expected vcpkg openssl:arm64-windows, set by the workflow)"; exit 1; }
  [ "${OPENSSL_STATIC:-}" = 1 ] || { echo "ERROR: OPENSSL_STATIC=1 not set (the shipped wheel must not depend on a runtime OpenSSL DLL)"; exit 1; }
  WITHOUT_CRYPTO="$TMP/requirements-$OS.no-cryptography.lock"
  awk '/^[A-Za-z0-9_.-]+==/{skip=($0 ~ /^cryptography==/)} {if(!skip) print}' \
      "$LOCKS/requirements-$OS.lock" > "$WITHOUT_CRYPTO"
  "$BPY" -m pip download --quiet --no-deps --only-binary=:all: --require-hashes \
      -r "$WITHOUT_CRYPTO" --dest "$WH"
  echo "   cryptography==50.0.1 has no win_arm64 wheel on PyPI: building from the sdist (PyPI-published hash) instead"
  printf 'cryptography==50.0.1 --hash=sha256:%s\n' \
      "5dd9bda1c12b4162f6ff568eeb5e0ff956c28d14406e875cfe8a63a2d414ff20" > "$TMP/cryptography-sdist.lock"
  "$BPY" -m pip wheel --quiet --no-deps --no-binary cryptography --require-hashes \
      -r "$TMP/cryptography-sdist.lock" --wheel-dir "$WH"
  CRYPTO_WHEEL="$(ls "$WH"/cryptography-50.0.1-*.whl)"
  CRYPTO_HASH="$("$BPY" -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$CRYPTO_WHEEL")"
  # Written into $WH (not $TMP) so build-sidecar.sh and build-notices.sh — separate script
  # invocations, later `run:` steps of the same job — can find it too, via common.sh's effective_lock().
  EFFECTIVE_LOCK="$WH/requirements-$OS.lock.effective"
  { cat "$WITHOUT_CRYPTO"; printf 'cryptography==50.0.1 --hash=sha256:%s\n' "$CRYPTO_HASH"; } > "$EFFECTIVE_LOCK"
else
  "$BPY" -m pip download --quiet --no-deps --only-binary=:all: --require-hashes \
      -r "$LOCKS/requirements-$OS.lock" --dest "$WH"
fi

echo "== 2/4 first-party git dependencies: clean verified clones -> wheels =="
"$BPY" -I "$PK/tools/build_git_wheels.py" --pins "$PK/git-pins.json" --out "$WH" \
    --record "$TMP/git-wheels.json" --python "$BPY" --work "$TMP"

echo "== 3/4 agentic-os wheel from the tracked files of this checkout =="
"$BPY" -I "$PK/tools/build_project_wheel.py" --root "$ROOT" --out "$WH" --python "$BPY"

echo "== 4/4 PROVE an offline, hash-checked install: exact set, no index, no git =="
uv venv --python "$PYTHON_VERSION" "$TMP/verify" >/dev/null
VPY="$(venv_py "$TMP/verify")"
uv pip install --python "$VPY" --no-index --find-links "$WH" --require-hashes --no-deps \
    -r "$EFFECTIVE_LOCK" >/dev/null
first_party_wheels "$BPY" "$WH"
# shellcheck disable=SC2086
uv pip install --python "$VPY" --no-index --find-links "$WH" --no-deps $FIRST >/dev/null
uv pip check --python "$VPY"
"$VPY" -c "import agentic_os, agentic_os.projects_api, agentic_os.mission.bootstrap; print('   offline import OK: control plane + bootstrap brain')"
"$VPY" - "$WH" <<'PY'
import importlib.metadata as m, pathlib, sys
wheels = {p.name.split("-")[0].lower().replace("_", "-") for p in pathlib.Path(sys.argv[1]).glob("*.whl")}
installed = {d.metadata["Name"].lower().replace("_", "-") for d in m.distributions()}
installed -= {"pip", "setuptools", "wheel"}
bad = [d.metadata["Name"] for d in m.distributions() if "vcs" in (d.read_text("direct_url.json") or "")]
print("   wheelhouse == installed set:", wheels == installed, "| VCS-origin distributions:", bad or "none")
sys.exit(0 if wheels == installed and not bad else 1)
PY
( cd "$WH" && "$BPY" - <<'PY'
import hashlib, pathlib
rows = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in sorted(pathlib.Path('.').glob('*.whl'))]
pathlib.Path('WHEELS.sha256').write_text("\n".join(rows) + "\n")
PY
)
cp "$TMP/git-wheels.json" "$WH/first-party-wheels.json"
"$BPY" -I "$PK/tools/build_manifest.py" --root "$ROOT" --out "$PK/dist/build-manifest.json" --stage wheelhouse --record-python \
    --wheelhouse "$WH" --git-wheels "$TMP/git-wheels.json" --cmd 'uv="uv --version"' --duration "wheelhouse=$(( $(now) - T0 ))"
echo
echo "wheelhouse: $("$BPY" -I "$PK/tools/first_party.py" summary --wheelhouse "$WH" --lock "$LOCKS/requirements-$OS.lock" | strip_cr) in $WH   (first-party set: packaging/git-pins.json + agentic-os)"
echo "OK — the suite installs fully offline from hash-checked wheels; git+https is gone from the end-user path."
