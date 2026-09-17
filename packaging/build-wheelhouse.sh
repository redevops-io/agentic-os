#!/usr/bin/env bash
# Task 1 — the reproducible wheelhouse: make the git+https dependencies DISAPPEAR from the
# end-user path. We build every dependency (incl. our own runtime-contracts / knowledge-frontier /
# redevops-connectors, which pyproject pins as git direct-URLs) into wheels, rewrite the project's
# direct-URL deps to plain names, and prove a fully offline `--no-index` install — no git, no network.
#
# Output: packaging/wheelhouse/*.whl  +  packaging/requirements.lock (exact pins)
# Target interpreter: CPython 3.12 (the bundle's runtime). Override with $PYTHON_VERSION.
# Runs on Linux, macOS (bash 3.2 — no mapfile) and Windows git-bash (venv bin dir = Scripts/, no rsync).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WH="$ROOT/packaging/wheelhouse"
PYV="${PYTHON_VERSION:-3.12}"
# The suite the native installer ships: Projects control plane + connectors + an embedded (DuckDB)
# durable ledger so missions survive a reboot with no external service. (rag/postgres/mcp excluded.)
EXTRAS="projects,duckdb"

# uv venv puts executables in bin/ on POSIX and Scripts/ on Windows — resolve per-venv.
venv_bin() { if [ -d "$1/bin" ]; then echo "$1/bin"; else echo "$1/Scripts"; fi; }

rm -rf "$WH"; mkdir -p "$WH"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "== build venv (seeded, python $PYV) =="
uv venv --seed --python "$PYV" "$TMP/build" >/dev/null
BBIN="$(venv_bin "$TMP/build")"; PIP="$BBIN/pip"; PY="$BBIN/python"

echo "== derive git deps from pyproject (core + EXTRAS: $EXTRAS) =="
# every git+https direct-URL dep in the core deps + the built EXTRAS — derived, so the list can't
# drift out of sync (that is exactly what stranded `knowledge-frontier`: a core git dep added later).
GIT_DEPS=()
while IFS= read -r line; do
  [ -n "$line" ] && GIT_DEPS+=("$line")
done < <("$PY" - "$ROOT/pyproject.toml" "$EXTRAS" <<'PY'
import sys, tomllib
d = tomllib.load(open(sys.argv[1], "rb"))
extras = [e for e in sys.argv[2].split(",") if e]
proj = d["project"]
deps = list(proj.get("dependencies", []))
opt = proj.get("optional-dependencies", {})
for e in extras:
    deps += opt.get(e, [])
seen = set()
for x in deps:
    if "git+" in x and x not in seen:
        seen.add(x); print(x)
PY
)
[ ${#GIT_DEPS[@]} -gt 0 ] || { echo "   ERROR: no git deps found in pyproject"; exit 1; }
printf '   %s\n' "${GIT_DEPS[@]}"

echo "== 1/4 build wheels for the git-pinned deps (they become normal versioned wheels) =="
"$PIP" wheel --wheel-dir "$WH" "${GIT_DEPS[@]}"

echo "== 2/4 sanitize the project: git+https direct refs -> plain names =="
# portable copy (rsync isn't on Windows git-bash): tar the project, excluding .git + the wheelhouse.
mkdir -p "$TMP/src"
( cd "$ROOT" && tar --exclude=.git --exclude=packaging/wheelhouse -cf - . ) | ( cd "$TMP/src" && tar -xf - )
"$PY" - "$TMP/src/pyproject.toml" <<'PY'
import re, sys
p = sys.argv[1]; t = open(p).read()
# "<name> @ git+https://...@ref"  ->  "<name>"  (wheelhouse supplies the built wheel)
t = re.sub(r'"([A-Za-z0-9_.-]+)\s*@\s*git\+https://[^"]+"', r'"\1"', t)
open(p, "w").write(t)
print("   sanitized direct-URL deps in pyproject.toml")
PY

echo "== 3/4 build wheels for the sanitized suite (resolves the git deps from the wheelhouse) =="
# On Windows git-bash the native pip.exe can't read the MSYS "/tmp/..[extras]" path (git-bash won't
# auto-convert an arg with [brackets]); hand pip a real Windows path via cygpath there. POSIX unchanged.
SRC="$TMP/src"
command -v cygpath >/dev/null 2>&1 && SRC="$(cygpath -m "$TMP/src")"
"$PIP" wheel --wheel-dir "$WH" --find-links "$WH" "${SRC}[$EXTRAS]"

echo "== 4/4 PROVE a fully offline install (no index, no git) + lock =="
uv venv --seed --python "$PYV" "$TMP/verify" >/dev/null
VBIN="$(venv_bin "$TMP/verify")"; VPIP="$VBIN/pip"; VPY="$VBIN/python"
# --no-index means pip may NOT reach any index or git; a surviving direct-URL dep would fail here.
"$VPIP" install --no-index --find-links "$WH" "agentic-os[$EXTRAS]" >/dev/null
"$VPIP" freeze > "$ROOT/packaging/requirements.lock"
"$VPY" -c "import agentic_os, agentic_os.projects_api, agentic_os.mission.bootstrap; print('   offline import OK: control plane + bootstrap brain')"
# assert nothing installed still carries a VCS/URL origin
if "$VPY" - <<'PY'
import importlib.metadata as m, sys
bad=[d.metadata["Name"] for d in m.distributions()
     if (d.read_text("direct_url.json") or "").find("vcs")>=0]
print("   VCS-origin distributions:", bad or "none")
sys.exit(1 if bad else 0)
PY
then :; else echo "   WARNING: a dependency still has a VCS origin"; fi

echo
echo "wheelhouse: $(ls "$WH"/*.whl | wc -l) wheels in $WH"
echo "lock:       packaging/requirements.lock"
echo "OK — the suite installs fully offline; git+https is gone from the end-user path."
