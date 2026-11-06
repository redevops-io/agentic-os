#!/bin/bash
# fallback_throwaway_home.sh: PREPARE and VERIFY a throwaway-HOME run inside the owner's own session. It never starts the
# app, never opens a window, and refuses to print the launch command unless the owner's explicit approval is acknowledged.
#
#   fallback_throwaway_home.sh prepare [--dir DIR]
#   fallback_throwaway_home.sh command --i-have-the-owners-explicit-approval --app "/path/Agentic Apps R0-prime.app" [--dir DIR]
#   fallback_throwaway_home.sh verify  [--dir DIR]
#
# This is NOT equivalent to a fresh account (README, "Fallback"): the keychain, the Gatekeeper first-launch path of a
# quarantined copy, LaunchServices registration and WebKit's per-user data under the REAL ~/Library are shared with the
# owner's session. It only isolates what HOME / TMPDIR / the working directory isolate. Use it only when the owner approves
# it explicitly; the owner (not this script) runs the printed command.
#   prepare: creates DIR/{home,tmp,cwd,evidence}, takes a BASELINE of the real HOME (post_run_checks snapshot) and of the
#            throwaway HOME, and prints what to do next. Idempotent (re-running keeps the first baseline).
#   command: prints the one command line the owner may run from Terminal (it starts the launcher binary directly with the
#            throwaway HOME/TMPDIR; the script does not run it).
#   verify:  after the owner quit the app: runs post_run_checks against the throwaway HOME and against the real HOME
#            (compared with the baseline), so leaks into the real ~/Library show up as new entries. Never deletes anything.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"

MODE="${1:-}"; [ $# -gt 0 ] && shift
DIR=""; APP=""; APPROVED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="${2:?}"; shift 2;;
    --app) APP="${2:?}"; shift 2;;
    --i-have-the-owners-explicit-approval) APPROVED=1; shift;;
    -h|--help) sed -n '2,/^set -u/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown argument: $1";;
  esac
done
[ "$(uname -s)" = Darwin ] || die "macOS only"
[ "$(id -u)" -ne 0 ] || die "do not run as root"
[ -n "$DIR" ] || DIR="$PWD/r0p-throwaway"
case "$MODE" in prepare|command|verify) ;; *) die "usage: $0 prepare|command|verify [options] (see --help)";; esac

case "$MODE" in
  prepare)
    mkdir -p "$DIR/home" "$DIR/tmp" "$DIR/cwd" "$DIR/evidence" || die "cannot create $DIR"
    DIR="$(abs_path "$DIR")"
    if [ -f "$DIR/evidence/baseline-real-home/snapshot.txt" ]; then echo "baseline already present, keeping it: $DIR/evidence/baseline-real-home"
    else
      "$HERE/post_run_checks.sh" --out "$DIR/evidence/baseline-real-home" >"$DIR/evidence/baseline-real-home.stdout.txt" 2>&1
      echo "baseline of the REAL home written: $DIR/evidence/baseline-real-home/snapshot.txt (post_run_checks exit $?)"
    fi
    cat <<EOT

Prepared: $DIR
  throwaway HOME : $DIR/home
  throwaway TMPDIR: $DIR/tmp
  working dir    : $DIR/cwd
Next (only with the owner's explicit approval): $0 command --i-have-the-owners-explicit-approval --app "<path to Agentic Apps R0-prime.app>" --dir "$DIR"
Then follow MANUAL-CHECKLIST.md as far as it applies, quit the app, and run: $0 verify --dir "$DIR"
EOT
    ;;
  command)
    [ "$APPROVED" = 1 ] || die "refusing: pass --i-have-the-owners-explicit-approval only after the owner approved a GUI run in their own session"
    [ -d "$DIR/home" ] || die "run 'prepare' first"
    [ -x "$APP/Contents/MacOS/$MAIN_EXE" ] || die "launcher not found in --app: $APP"
    DIR="$(abs_path "$DIR")"; APP="$(abs_path "$APP")"
    cat <<EOT
The owner may run this in Terminal (it starts the launcher binary directly, in the owner's GUI session, with a throwaway HOME):

  cd "$DIR/cwd" && env HOME="$DIR/home" TMPDIR="$DIR/tmp" "$APP/Contents/MacOS/$MAIN_EXE"

Notes: launching the binary directly skips LaunchServices, so it is NOT the Finder first-launch / Gatekeeper path. Cocoa/WebKit
may still write under the real ~/Library (NSHomeDirectory follows the account database, not \$HOME); 'verify' shows what leaked.
Quit the app with Cmd+Q (or Ctrl+C in that Terminal) when done.
EOT
    ;;
  verify)
    [ -d "$DIR/home" ] || die "no throwaway directory at $DIR (run 'prepare')"
    DIR="$(abs_path "$DIR")"; rc=0
    mkdir -p "$DIR/evidence"
    S="$(utc_stamp)"
    echo "== throwaway HOME ($DIR/home), temp dir $DIR/tmp"
    "$HERE/post_run_checks.sh" --home "$DIR/home" --tmp "$DIR/tmp" --out "$DIR/evidence/post-throwaway-$S" || rc=1
    echo; echo "== real HOME, compared with the baseline"
    BASE="$DIR/evidence/baseline-real-home/snapshot.txt"
    if [ -f "$BASE" ]; then "$HERE/post_run_checks.sh" --out "$DIR/evidence/post-real-$S" --compare "$BASE" || rc=1
    else "$HERE/post_run_checks.sh" --out "$DIR/evidence/post-real-$S" || rc=1; echo "(no baseline: run 'prepare' BEFORE the first launch)"; fi
    exit "$rc"
    ;;
esac
