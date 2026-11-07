#!/bin/bash
# Run pytest in an environment made by make_env.sh, under `env -i` with an allowlist. Refuses to run when env.json does not belong to this worktree.
#
#   scripts/testenv/run_tests.sh --env-dir DIR [--worktree PATH] [--tmpdir DIR] [--env NAME=VALUE]... [--] [pytest args]   (default pytest args: -q tests)
#
# Allowlist: PATH (venv/bin first), HOME=DIR/home, TMPDIR=DIR/tmp (or --tmpdir), LANG=C.UTF-8, PYTHONDONTWRITEBYTECODE=1, plus any --env pairs.
# Nothing else is inherited (the caller's credentials never reach pytest); PYTHONPATH, PYTHONHOME and VIRTUAL_ENV are refused as --env.
# Gate: env.json's worktree must be this worktree, its recorded git HEAD must be an ancestor of (or equal to) this worktree's HEAD, and agentic_os must
# still resolve inside this worktree from a neutral cwd and from a nested subprocess.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
fail() { echo "run_tests: $*" >&2; exit 1; }

ENVDIR="" WT="$(cd "$HERE/../.." && pwd -P)" TMPD="" EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --env-dir) ENVDIR="${2:?}"; shift 2 ;;
    --worktree) WT="$(cd "${2:?}" && pwd -P)"; shift 2 ;;
    --tmpdir) TMPD="${2:?}"; shift 2 ;;
    --env) case "${2:?}" in PYTHONPATH=*|PYTHONHOME=*|VIRTUAL_ENV=*) fail "refusing --env $2: the binding must come from the editable install" ;; esac; EXTRA+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}"; exit 0 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
[ -n "$ENVDIR" ] || fail "--env-dir is required (the directory given to make_env.sh)"
ENVDIR="$(cd "$ENVDIR" && pwd -P)"
VPY="$ENVDIR/venv/bin/python"
[ -x "$VPY" ] || fail "$VPY does not exist: run make_env.sh first"
TMPD="${TMPD:-$ENVDIR/tmp}"; mkdir -p "$TMPD" "$ENVDIR/home"
[ $# -gt 0 ] || set -- -q tests

CLEAN=(env -i "PATH=$ENVDIR/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" "HOME=$ENVDIR/home" "TMPDIR=$TMPD" LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1)
"${CLEAN[@]}" "$VPY" "$HERE/envtool.py" check --env-dir "$ENVDIR" --worktree "$WT" || exit 1
cd "$WT"
exec "${CLEAN[@]}" ${EXTRA[@]+"${EXTRA[@]}"} "$VPY" -m pytest "$@"
