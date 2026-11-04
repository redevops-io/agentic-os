#!/usr/bin/env bash
# Task 1 — the reproducible wheelhouse: every shipped Python component as a wheel, from PINNED inputs.
#
#   third-party (21)  pip download --only-binary --require-hashes -r locks/requirements-<os>.lock
#   first-party (4)   built from CLEAN, verified clones of the pinned tags (runtime-contracts,
#                     knowledge-frontier, redevops-connectors: packaging/git-pins.json) and from the
#                     tracked files of this checkout (agentic-os), with backends installed from
#                     locks/build-backends-<os>.lock and `--no-build-isolation`
#
# No unpinned build input remains: the interpreter must be exactly CPython $PYTHON_VERSION, pip/
# setuptools/wheel/hatchling come from a hash lock, the clones are checked against tag object AND
# commit, the committed stale build/ of runtime-contracts v0.3.4 is removed, and every built .py is
# compared with `git show <commit>:<path>`.
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

echo "== pins check (pyproject <-> git-pins.json <-> requirements.in <-> locks) =="
python3 -I "$PK/verify-pins.py" --quiet || { echo "pins are inconsistent: see packaging/verify-pins.py"; exit 1; }

echo "== build venv: CPython $PYTHON_VERSION (unseeded) + build backends from the hash lock =="
uv venv --python "$PYTHON_VERSION" "$TMP/build" >/dev/null
BPY="$(venv_py "$TMP/build")"
assert_python "$BPY"
uv pip install --python "$BPY" --require-hashes --no-deps -r "$LOCKS/build-backends-$OS.lock" >/dev/null
echo "   $("$BPY" -m pip --version | cut -d' ' -f1-2), setuptools $("$BPY" -c 'import setuptools;print(setuptools.__version__)')"
[ "$("$BPY" -m pip --version | cut -d' ' -f2)" = "$PIP_VERSION" ] || { echo "pip is not the pinned $PIP_VERSION"; exit 1; }

echo "== 1/4 third-party wheels: pip download --require-hashes (binary only, no build) =="
"$BPY" -m pip download --quiet --no-deps --only-binary=:all: --require-hashes \
    -r "$LOCKS/requirements-$OS.lock" --dest "$WH"

echo "== 2/4 first-party git dependencies: clean verified clones -> wheels =="
"$BPY" -I "$PK/tools/build_git_wheels.py" --pins "$PK/git-pins.json" --out "$WH" \
    --record "$TMP/git-wheels.json" --python "$BPY" --work "$TMP"

echo "== 3/4 agentic-os wheel from the tracked files of this checkout =="
"$BPY" -I "$PK/tools/build_project_wheel.py" --root "$ROOT" --out "$WH" --python "$BPY"

echo "== 4/4 PROVE an offline, hash-checked install: exact set, no index, no git =="
uv venv --python "$PYTHON_VERSION" "$TMP/verify" >/dev/null
VPY="$(venv_py "$TMP/verify")"
uv pip install --python "$VPY" --no-index --find-links "$WH" --require-hashes --no-deps \
    -r "$LOCKS/requirements-$OS.lock" >/dev/null
FIRST="$(ls "$WH"/agentic_os-*.whl "$WH"/runtime_contracts-*.whl "$WH"/knowledge_frontier-*.whl "$WH"/redevops_connectors-*.whl)"
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
echo "wheelhouse: $(ls "$WH"/*.whl | wc -l | tr -d ' ') wheels in $WH   (expected 25 = 21 third-party + 4 first-party)"
echo "OK — the suite installs fully offline from hash-checked wheels; git+https is gone from the end-user path."
