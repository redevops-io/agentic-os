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
# Windows (git-bash): a native Windows program writes text with CRLF line ends EVEN INTO A PIPE (Python's print() included),
# and bash's $(...) strips only the "\n", so a captured version string or path would end in "\r" and compare unequal / name a
# directory that does not exist. Pipe every captured native-program output that is compared or used as a path through strip_cr.
strip_cr() { tr -d '\r'; }
# Windows (git-bash): some scripts under packaging/ call a bare `python3` (packaging/notices/build-licenses.sh). A Windows
# CPython installation provides `python`, and `python3` may be missing or be the Microsoft Store alias stub. When no working
# python3 exists, put a one-line wrapper named python3 in front of PATH (inherited by every child script).
if [ "$OS" = windows ] && ! python3 -c 'import sys' >/dev/null 2>&1; then
  mkdir -p "$PK/dist/.pyshim"
  printf '#!/usr/bin/env bash\nexec python "$@"\n' > "$PK/dist/.pyshim/python3"
  chmod +x "$PK/dist/.pyshim/python3"
  PATH="$PK/dist/.pyshim:$PATH"; export PATH
fi
# The interpreter must be EXACTLY the pinned CPython. (uv matches "3.12.10" exactly; this re-checks.)
assert_python() {
  local got; got="$("$1" -c 'import platform;print(platform.python_version())' | strip_cr)"
  [ "$got" = "$PYTHON_VERSION" ] || { echo "ERROR: interpreter is $got, pinned CPython is $PYTHON_VERSION"; exit 1; }
}
# Run a command with a SCRUBBED environment (nothing of the build host's environment reaches a frozen binary under test).
#   scrubbed_run <scratch dir> <command> [args...]
# Windows needs more than HOME/TMPDIR: the frozen exe extracts into %TEMP% (else the Windows directory), and Winsock/crypto
# need SYSTEMROOT. The values handed to a native program must be Windows paths (cygpath -w), not git-bash /tmp/... paths.
scrubbed_run() {
  local t="$1"; shift
  if [ "$OS" = windows ]; then
    local w; w="$(cygpath -w "$t" | strip_cr)"
    env -i PATH="$PATH" HOME="$t" TMPDIR="$t" TEMP="$w" TMP="$w" USERPROFILE="$w" LOCALAPPDATA="$w" APPDATA="$w" \
        SYSTEMROOT="${SYSTEMROOT:-${SystemRoot:-C:\\Windows}}" "$@"
  else
    env -i PATH="$PATH" HOME="$t" TMPDIR="$t" "$@"
  fi
}
now() { date +%s; }
