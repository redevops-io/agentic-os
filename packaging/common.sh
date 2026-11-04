#!/usr/bin/env bash
# Shared helpers for the packaging scripts (sourced, not executed). Bash 3.2 / git-bash safe.
# Provides: ROOT, PK, OS (macos|windows|linux), the pinned versions from toolchain.env, venv_bin, and
# assert_python (the interpreter must be exactly PYTHON_VERSION).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PK="$ROOT/packaging"
set -a; . "$PK/toolchain.env"; set +a
case "$(uname -s)" in
  Darwin) OS=macos ;;
  MINGW*|MSYS*|CYGWIN*) OS=windows ;;
  Linux) OS=linux ;;
  *) echo "unsupported OS $(uname -s)"; exit 1 ;;
esac
LOCKS="$PK/locks"
# uv venv puts executables in bin/ on POSIX and Scripts/ on Windows git-bash.
venv_bin() { if [ -d "$1/bin" ]; then echo "$1/bin"; else echo "$1/Scripts"; fi; }
venv_py()  { local b; b="$(venv_bin "$1")"; if [ -x "$b/python" ]; then echo "$b/python"; else echo "$b/python.exe"; fi; }
# The interpreter must be EXACTLY the pinned CPython. (uv matches "3.12.10" exactly; this re-checks.)
assert_python() {
  local got; got="$("$1" -c 'import platform;print(platform.python_version())')"
  [ "$got" = "$PYTHON_VERSION" ] || { echo "ERROR: interpreter is $got, pinned CPython is $PYTHON_VERSION"; exit 1; }
}
now() { date +%s; }
# identity KEY: one field of the R0-prime identity (packaging/identity.json via packaging/tools/identity.py), e.g.
# `identity main_binary_name`, `identity app_bundle`, `identity dmg_macos_arm64`. Standard-library python, no side effect.
identity() { "$(command -v python3 || command -v python)" -I "$PK/tools/identity.py" get "$1" | tr -d '\r'; }
