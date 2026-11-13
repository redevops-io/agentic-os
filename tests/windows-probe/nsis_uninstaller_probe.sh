#!/usr/bin/env bash
# usage: nsis_uninstaller_probe.sh <workspace> <scratch dir> <github event name>
# Runs on windows-latest (Git Bash) with NSIS installed. Builds a tiny installer twice (when the base helper differs) and
# checks what the helper's COLLECT mode recorded for the uninstaller makensis generated.
set -euo pipefail
WS="$1"; SCRATCH="$2"; EVENT="${3:-}"
MAKENSIS="C:\\Program Files (x86)\\NSIS\\makensis.exe"
BASH_EXE="$(cygpath -w "$(command -v bash)")"
HELPER_REL="packaging/signing/r0prime_win_signcommand.sh"

build_and_collect() { # label helper-file -> sets REQ_DIR, prints the log
  local label="$1" helper="$2" work="$SCRATCH/probe-$1"
  mkdir -p "$work/handoff"
  local out="$(cygpath -w "$work/probe-setup.exe")"
  sed -e "s|@OUT@|$out|" -e "s|@BASH@|$BASH_EXE|" -e "s|@HELPER@|$(cygpath -w "$helper")|" \
      "$WS/tests/windows-probe/probe.nsi.in" > "$work/probe.nsi"
  echo "::group::makensis ($label)"
  # R0P_* are read by the helper through the inherited environment
  ( export R0P_HANDOFF_MODE=collect R0P_HANDOFF_DIR="$(cygpath -w "$work/handoff")"
    MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' "$(cygpath -u "C:\\Program Files (x86)\\NSIS\\makensis.exe")" -V3 "$(cygpath -w "$work/probe.nsi")" ) || echo "makensis exit code: $? (not decisive; the recorded requests are)"
  echo "::endgroup::"
  test -f "$work/probe-setup.exe" || { echo "::error::makensis produced no installer ($label)"; return 1; }
  echo "requests-inner ($label):"; ls -l "$work/handoff/requests-inner" 2>/dev/null || echo "  (none)"
  echo "signcommand.log ($label):"; cat "$work/handoff/signcommand.log" 2>/dev/null || echo "  (none)"
}

uninstaller_requests() { ls "$SCRATCH/probe-$1/handoff/requests-inner" 2>/dev/null | grep -E '^uninstaller-[0-9a-f]{64}\.exe$' || true; }

# ---- before: the helper as it is on the base branch (pull requests only)
if [ "$EVENT" = pull_request ] && git -C "$WS" cat-file -e "HEAD^1:$HELPER_REL" 2>/dev/null; then
  mkdir -p "$SCRATCH/base-helper"
  git -C "$WS" show "HEAD^1:$HELPER_REL" > "$SCRATCH/base-helper/r0prime_win_signcommand.sh"
  if grep -q 'nst\[0-9A-Fa-f\]\*\.tmp' "$SCRATCH/base-helper/r0prime_win_signcommand.sh"; then
    echo "::notice::the base branch helper already handles nstXXXX.tmp; no before leg"
  else
    build_and_collect before "$SCRATCH/base-helper/r0prime_win_signcommand.sh" || true
    if [ -n "$(uninstaller_requests before)" ]; then echo "::warning::the base helper unexpectedly collected the uninstaller"
    else echo "BEFORE: the base helper recorded NO uninstaller request; that installer would ship its uninstaller unsigned"; fi
  fi
fi

# ---- after: the helper in this checkout
build_and_collect after "$WS/$HELPER_REL"
got="$(uninstaller_requests after)"
[ "$(printf '%s\n' "$got" | grep -c .)" = 1 ] || { echo "::error::expected exactly one uninstaller-<sha256>.exe in requests-inner, got: [$got]"; exit 1; }
f="$SCRATCH/probe-after/handoff/requests-inner/$got"
want="${got#uninstaller-}"; want="${want%.exe}"
have="$(sha256sum "$f" | cut -d' ' -f1)"
[ "$want" = "$have" ] || { echo "::error::file name hash $want differs from content hash $have"; exit 1; }
[ "$(head -c 2 "$f")" = "MZ" ] || { echo "::error::the collected file is not a PE executable"; exit 1; }
grep -q 'role=uninstaller' "$SCRATCH/probe-after/handoff/signcommand.log" || { echo "::error::signcommand.log has no role=uninstaller line"; exit 1; }
echo "AFTER: collected $got (sha256 matches content, role=uninstaller)"
