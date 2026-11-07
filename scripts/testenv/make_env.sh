#!/bin/bash
# Create a clean test environment BOUND to one worktree.
#
#   scripts/testenv/make_env.sh --worktree PATH --env-dir DIR [--python PY] [--side-deps DIR] [--force]
#   scripts/testenv/make_env.sh --worktree PATH --env-dir DIR --dry-run        print the plan, create nothing
#   scripts/testenv/make_env.sh --worktree PATH --env-dir DIR --verify-only    only check that DIR/venv is bound to PATH
#
# DIR must be OUTSIDE the worktree. It receives venv/ (the interpreter), env.json (the record), home/ and tmp/ (throwaway HOME and TMPDIR for run_tests.sh).
# Network: only the package index (or the pip cache). The three dependencies the pyproject pins by git tag are NOT fetched; supply them with --side-deps
# (a directory laid out like site-packages holding <name>-<version>.dist-info and the package, the exact pinned versions). No credentials are used.
# Run it under `env -i` with PATH and a throwaway HOME; PYTHONPATH is refused because the binding must come from the editable install.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
fail() { echo "make_env: $*" >&2; exit 1; }

WT="" ENVDIR="" PY="" SIDE="" FORCE=0 DRY=0 VERIFY_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --worktree) WT="${2:?}"; shift 2 ;;
    --env-dir) ENVDIR="${2:?}"; shift 2 ;;
    --python) PY="${2:?}"; shift 2 ;;
    --side-deps) SIDE="${2:?}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done
[ -n "$WT" ] && [ -n "$ENVDIR" ] || fail "--worktree and --env-dir are required (see --help)"
[ -z "${PYTHONPATH:-}" ] || fail "PYTHONPATH is set (${PYTHONPATH}); run this under 'env -i' with an allowlist: the binding must come from the editable install, never from PYTHONPATH"

[ -d "$WT" ] || fail "worktree $WT does not exist"
WT="$(cd "$WT" && pwd -P)"
[ -f "$WT/pyproject.toml" ] && [ -d "$WT/agentic_os" ] || fail "$WT is not an agentic-os checkout (no pyproject.toml / agentic_os/)"
mkdir -p "$ENVDIR"
ENVDIR="$(cd "$ENVDIR" && pwd -P)"
case "$ENVDIR/" in "$WT"/*) fail "--env-dir $ENVDIR is inside the worktree $WT; it must be outside the repository" ;; esac
VENV="$ENVDIR/venv"

if [ "$VERIFY_ONLY" = 1 ]; then
  [ -x "$VENV/bin/python" ] || fail "$VENV/bin/python does not exist"
  exec "$VENV/bin/python" "$HERE/envtool.py" verify --python "$VENV/bin/python" --worktree "$WT"
fi

# the interpreter (>= 3.11)
if [ -z "$PY" ]; then
  for c in python3.12 python3.13 python3.14 python3.11 python3; do
    p="$(command -v "$c" 2>/dev/null || true)"
    [ -n "$p" ] && "$p" -c 'import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null && { PY="$p"; break; }
  done
fi
[ -n "$PY" ] || fail "no Python >= 3.11 found on PATH; pass --python"
"$PY" -c 'import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)' || fail "$PY is older than Python 3.11"

PLAN="$("$PY" "$HERE/envtool.py" plan --worktree "$WT")"
if [ "$DRY" = 1 ]; then
  echo "worktree:  $WT"; echo "env dir:   $ENVDIR"; echo "python:    $PY"; echo "side-deps: ${SIDE:-<none given>}"; echo "plan:      $PLAN"
  exit 0
fi

GIT_COUNT="$("$PY" -c 'import json,sys;print(len(json.loads(sys.argv[1])["git"]))' "$PLAN")"
[ "$GIT_COUNT" = 0 ] || [ -n "$SIDE" ] || fail "the pyproject pins $GIT_COUNT dependencies by git tag; pass --side-deps DIR (see --help). They are not fetched from the network."
[ -z "$SIDE" ] || [ -d "$SIDE" ] || fail "--side-deps $SIDE is not a directory"

if [ -e "$VENV" ]; then
  [ "$FORCE" = 1 ] || fail "$VENV already exists (use --force to recreate it)"
  [ -f "$VENV/pyvenv.cfg" ] || fail "$VENV exists but is not a virtual environment; refusing to remove it"
  rm -rf "$VENV"
fi
mkdir -p "$ENVDIR/home" "$ENVDIR/tmp"

"$PY" -m venv "$VENV"
VPY="$VENV/bin/python"
REQ="$ENVDIR/requirements.index.txt"; CON="$ENVDIR/constraints.uv-lock.txt"
"$PY" -c 'import json,sys;p=json.loads(sys.argv[1]);open(sys.argv[2],"w").write("\n".join(p["index"])+"\n");open(sys.argv[3],"w").write("\n".join(p["constraints"])+"\n")' "$PLAN" "$REQ" "$CON"

echo "make_env: installing the index requirements (dev extra + base), constrained to the versions in uv.lock"
"$VPY" -m pip install --disable-pip-version-check --quiet -c "$CON" -r "$REQ"
[ -z "$SIDE" ] || "$VPY" "$HERE/envtool.py" side --python "$VPY" --worktree "$WT" --side-deps "$SIDE"
echo "make_env: installing $WT editable (no dependencies: they are installed above)"
"$VPY" -m pip install --disable-pip-version-check --quiet --no-deps -e "$WT"

"$VPY" "$HERE/envtool.py" verify --python "$VPY" --worktree "$WT"
"$VPY" "$HERE/envtool.py" write --python "$VPY" --worktree "$WT" --env-dir "$ENVDIR"

cat <<MSG

make_env: ready.
  env.json:  $ENVDIR/env.json
  run the suite (env -i allowlist, refuses a mismatched worktree/HEAD):
    $HERE/run_tests.sh --env-dir $ENVDIR -- tests -q
MSG
