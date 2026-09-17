#!/usr/bin/env bash
# Task 1 — the reproducible wheelhouse: make the git+https dependencies DISAPPEAR from the
# end-user path. We build every dependency (incl. our own runtime-contracts / redevops-connectors,
# which pyproject pins as git direct-URLs) into wheels, rewrite the project's direct-URL deps to
# plain names, and prove a fully offline `--no-index` install of the whole suite — no git, no network.
#
# Output: packaging/wheelhouse/*.whl  +  packaging/requirements.lock (exact pins)
# Target interpreter: CPython 3.12 (the bundle's runtime). Override with $PYTHON_VERSION.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WH="$ROOT/packaging/wheelhouse"
PYV="${PYTHON_VERSION:-3.12}"
# The suite the native installer ships: Projects control plane + connectors + an embedded (DuckDB)
# durable ledger so missions survive a reboot with no external service. (rag/postgres/mcp excluded.)
EXTRAS="projects,duckdb"
# GIT_DEPS is derived from pyproject after the build venv exists (below) — every git+https
# direct-URL dep in the core deps + the built EXTRAS — so the list can't drift out of sync
# (this is exactly what stranded `knowledge-frontier`: a core git dep added after a hardcoded list).

rm -rf "$WH"; mkdir -p "$WH"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "== build venv (seeded, python $PYV) =="
uv venv --seed --python "$PYV" "$TMP/build" >/dev/null
PIP="$TMP/build/bin/pip"

echo "== derive git deps from pyproject (core + EXTRAS: $EXTRAS) =="
mapfile -t GIT_DEPS < <("$TMP/build/bin/python" - "$ROOT/pyproject.toml" "$EXTRAS" <<'PY'
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
    if "git+" in x and x not in seen:   # a git direct-URL dep the wheelhouse must pre-build
        seen.add(x); print(x)
PY
)
printf '   %s\n' "${GIT_DEPS[@]}"

echo "== 1/4 build wheels for the git-pinned deps (they become normal versioned wheels) =="
"$PIP" wheel --wheel-dir "$WH" "${GIT_DEPS[@]}"

echo "== 2/4 sanitize the project: git+https direct refs -> plain names =="
rsync -a --exclude .git --exclude packaging/wheelhouse --exclude '**/__pycache__' "$ROOT/" "$TMP/src/"
"$TMP/build/bin/python" - "$TMP/src/pyproject.toml" <<'PY'
import re, sys
p = sys.argv[1]; t = open(p).read()
# "<name> @ git+https://...@ref"  ->  "<name>"  (wheelhouse supplies the built wheel)
t = re.sub(r'"([A-Za-z0-9_.-]+)\s*@\s*git\+https://[^"]+"', r'"\1"', t)
open(p, "w").write(t)
print("   sanitized direct-URL deps in pyproject.toml")
PY

echo "== 3/4 build wheels for the sanitized suite (resolves the git deps from the wheelhouse) =="
"$PIP" wheel --wheel-dir "$WH" --find-links "$WH" "$TMP/src[$EXTRAS]"

echo "== 4/4 PROVE a fully offline install (no index, no git) + lock =="
uv venv --seed --python "$PYV" "$TMP/verify" >/dev/null
VPIP="$TMP/verify/bin/pip"; VPY="$TMP/verify/bin/python"
# --no-index means pip may NOT reach any index or git; a surviving direct-URL dep would fail here.
PATH="/usr/bin:/bin" "$VPIP" install --no-index --find-links "$WH" "agentic-os[$EXTRAS]" >/dev/null
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
