#!/bin/bash
# post_run_checks.sh: what is left running / listening / on disk for the CURRENT user after a run of Agentic Apps.
#
#   post_run_checks.sh [--phase stopped|running] [--home DIR] [--tmp DIR]... [--port N] [--out DIR]
#                      [--compare PREVIOUS-snapshot.txt]
#
# --phase stopped (default): after Stop / Quit / kill -9 / uninstall nothing of the app may run or listen, and no
#                            PyInstaller _MEI* directory may remain. Any process, listener or _MEI is a FAIL.
# --phase running:           after Start: the sidecar and its listener ARE expected (rows turn into PASS when present).
# --home DIR:   the HOME to inspect for leftovers (default $HOME). Use the throwaway HOME of a fallback run.
# --tmp DIR:    extra temp directories to scan for _MEI* (repeatable). Scanned by default: $TMPDIR, /tmp,
#               the per-user /private/var/folders/*/*/T and /C directories that belong to this user.
# --port N:     additionally require that nothing listens on 127.0.0.1:N.
# --compare F:  show what is new/gone against the snapshot.txt of an earlier run (run once BEFORE the first launch).
#
# Read-only: it lists, never deletes. It does NOT touch the keychain (it never calls `security`, and does not open
# ~/Library/Keychains). Leftover files under ~/Library are reported as WARN, never deleted: whether user data may
# legitimately remain after an uninstall is an owner decision (README).
# Exit status: 0 no FAIL, 1 at least one FAIL, 2 usage error.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"

PHASE="stopped"; HOME_DIR="$HOME"; EXTRA_TMP=""; PORT=""; OUT=""; COMPARE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --phase) PHASE="${2:?}"; shift 2;;
    --home) HOME_DIR="${2:?}"; shift 2;;
    --tmp) EXTRA_TMP="${EXTRA_TMP}${2:?}
"; shift 2;;
    --port) PORT="${2:?}"; shift 2;;
    --out) OUT="${2:?}"; shift 2;;
    --compare) COMPARE="${2:?}"; shift 2;;
    -h|--help) sed -n '2,/^set -u/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown argument: $1";;
  esac
done
case "$PHASE" in stopped|running) ;; *) die "--phase must be stopped or running";; esac
[ "$(uname -s)" = Darwin ] || die "macOS only"
[ "$(id -u)" -ne 0 ] || die "do not run as root (it must see exactly what the test user sees)"
[ -d "$HOME_DIR" ] || die "--home is not a directory: $HOME_DIR"
HOME_DIR="$(abs_path "$HOME_DIR")"
[ -n "$OUT" ] || OUT="$PWD/r0p-out/post-$(utc_stamp)"
if [ -e "$OUT" ]; then [ -d "$OUT" ] && [ -z "$(ls -A "$OUT")" ] || die "--out exists and is not empty: $OUT"; fi
mkdir -p "$OUT" || die "cannot create $OUT"
OUT="$(abs_path "$OUT")"
TRANSCRIPT="$OUT/post-run-transcript.txt"; : >"$TRANSCRIPT"
SNAP="$OUT/snapshot.txt"; : >"$SNAP"
snap() { printf '%s\n' "$*" >>"$SNAP"; }

header "post_run_checks (phase $PHASE)"
L "inspected HOME: $HOME_DIR"

# ---------------------------------------------------------------- 1. processes
# Match the EXECUTABLE (ps comm = full path of the image), not the whole command line: another tool's command line may
# merely mention these names (a shell, an editor, an agent). The task text asked for pgrep; pgrep -x/-f cannot do this
# reliably on a shared machine, so the same two names are matched through ps and pgrep is used as a cross-check below.
SELF_PIDS=" $$ "; p=$$
while [ "${p:-0}" -gt 1 ]; do p="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"; [ -n "$p" ] && SELF_PIDS="$SELF_PIDS$p "; done
PROCS=""
for pid in $(ps -ax -o uid=,pid=,comm= 2>/dev/null | awk -v u="$(id -u)" '$1==u {c=$0; sub(/^[ ]*[0-9]+[ ]+[0-9]+[ ]+/,"",c); b=c; sub(/.*\//,"",b); if (b=="redevops-sidecar" || b=="agentic-r0prime" || c ~ /\/Agentic Apps R0-prime\.app\//) print $2}'); do
  case "$SELF_PIDS" in *" $pid "*) continue;; esac
  line="$(ps -o pid=,ppid=,pgid=,etime=,command= -p "$pid" 2>/dev/null)"
  [ -n "$line" ] && PROCS="${PROCS}${line}
"
done
PGREP_N="$(pgrep -u "$(id -u)" -x redevops-sidecar 2>/dev/null | grep -c .) redevops-sidecar, $(pgrep -u "$(id -u)" -x "$MAIN_EXE" 2>/dev/null | grep -c .) $MAIN_EXE"
NPROC="$(printf '%s' "$PROCS" | grep -c .)"
L ""; L "=== processes (user $(id -un)): executables redevops-sidecar, $MAIN_EXE, anything inside $APP_NAME"
L "    cross-check pgrep -x: $PGREP_N"
NFROZEN_PROC="$(pgrep -u "$(id -u)" -x "$FROZEN_MAIN_EXE" 2>/dev/null | grep -c .)"
[ "$NFROZEN_PROC" -gt 0 ] && rec INFO PR-00 "$NFROZEN_PROC process(es) named $FROZEN_MAIN_EXE (the FROZEN v0.3.2 launcher) are running: not part of this candidate, not counted below; close them before judging the R0-prime result"
[ "$NPROC" -gt 0 ] && printf '%s' "$PROCS" | sed 's/^/    /' | tee_t
if [ "$PHASE" = stopped ]; then
  [ "$NPROC" -eq 0 ] && rec PASS PR-01 "no $MAIN_EXE / redevops-sidecar process" || rec FAIL PR-01 "$NPROC process(es) still running (a onefile sidecar is TWO processes: bootloader + Python child)"
else
  [ "$NPROC" -gt 0 ] && rec PASS PR-01 "$NPROC app process(es) running (phase running)" || rec FAIL PR-01 "nothing running although phase is running"
fi
snap "processes: $NPROC"

# ---------------------------------------------------------------- 2. listeners
L ""; L "=== TCP listeners of this user: lsof -nP -iTCP -sTCP:LISTEN"
LSOF="$(lsof -nP -a -u "$(id -u)" -iTCP -sTCP:LISTEN 2>&1)"
printf '%s\n' "$LSOF" >"$OUT/lsof-listen-all.txt"
L "    $(printf '%s\n' "$LSOF" | awk 'NR>1' | grep -c .) listener(s) in total for this user (full list only in $OUT/lsof-listen-all.txt; only the app's are shown here)"
APPL="$(printf '%s\n' "$LSOF" | awk 'NR>1 && ($1 ~ /redevops|agentic|Agentic/) {print}')"
# a Python child of the onefile sidecar shows up under the executable name of the bootloader (redevops-sidecar), but be
# defensive: also match the listener PIDs against the process list above.
for pid in $(printf '%s' "$PROCS" | awk '{print $1}'); do
  hit="$(printf '%s\n' "$LSOF" | awk -v p="$pid" 'NR>1 && $2==p {print}')"
  [ -n "$hit" ] && APPL="${APPL}
${hit}"
done
APPL="$(printf '%s\n' "$APPL" | sort -u | grep .)"
NL="$(printf '%s' "$APPL" | grep -c .)"
if [ "$PHASE" = stopped ]; then
  [ "$NL" -eq 0 ] && rec PASS LS-01 "no listener owned by an app process" || { rec FAIL LS-01 "$NL app listener(s):"; printf '%s\n' "$APPL" | sed 's/^/    /' | tee_t; }
else
  [ "$NL" -gt 0 ] && rec PASS LS-01 "app listener present (phase running)" || rec FAIL LS-01 "no app listener although phase is running"
  printf '%s\n' "$APPL" | grep -E ' (\*|0\.0\.0\.0|\[::\]):[0-9]+ ' >/dev/null 2>&1 && rec FAIL LS-02 "an app listener is bound to ALL interfaces (must be 127.0.0.1 only)" || rec PASS LS-02 "no app listener bound to all interfaces"
fi
if [ -n "$PORT" ]; then
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then rec FAIL LS-03 "something still listens on port $PORT"; else rec PASS LS-03 "nothing listens on port $PORT"; fi
fi
if [ "$PHASE" = stopped ] && lsof -nP -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1; then rec WARN LS-04 "something listens on the v0.3.2 default port 8787 (a stale v0.3.2 sidecar? it answers unauthenticated)"; fi
snap "app listeners: $NL"

# ---------------------------------------------------------------- 3. PyInstaller extraction directories
L ""; L "=== _MEI* extraction directories"
TMPS="${TMPDIR:-/tmp}
/tmp
/private/tmp
$EXTRA_TMP"
for d in /private/var/folders/*/*/T /private/var/folders/*/*/C /private/var/folders/*/*/0; do
  [ -d "$d" ] && [ -O "$d" ] && TMPS="${TMPS}${d}
"
done
SEEN=""; NMEI=0
while IFS= read -r d; do
  [ -n "$d" ] && [ -d "$d" ] || continue
  d="$(cd "$d" && pwd -P)"
  case "$SEEN" in *"|$d|"*) continue;; esac; SEEN="$SEEN|$d|"
  for m in "$d"/_MEI*; do
    [ -d "$m" ] || continue
    NMEI=$((NMEI + 1)); L "    $m ($(du -sk "$m" 2>/dev/null | awk '{print $1}') KB, owner $(stat -f %Su "$m"), mtime $(stat -f %Sm -t %Y-%m-%dT%H:%M:%S "$m"))"
    snap "mei: $m"
  done
done <<DIRS
$TMPS
DIRS
L "    scanned $(printf '%s' "$SEEN" | tr '|' '\n' | grep -c .) temp directories"
if [ "$PHASE" = stopped ]; then
  [ "$NMEI" -eq 0 ] && rec PASS TM-01 "no _MEI* directory" || rec FAIL TM-01 "$NMEI _MEI* directory(ies) left behind (80 MB each; only a hard kill of the whole process group leaves one)"
else
  [ "$NMEI" -gt 0 ] && rec PASS TM-01 "$NMEI _MEI* extraction dir(s) present (phase running)" || rec INFO TM-01 "no _MEI* found (it may live in a temp dir not scanned)"
fi

# ---------------------------------------------------------------- 4. leftovers in ~/Library and the home folder
L ""; L "=== leftovers under $HOME_DIR/Library for $APP_ID (and name variants: the frozen name agentic-apps, agentic-r0prime, Agentic Apps, redevops-launcher = the crate name used by unsigned dev builds)"
NLEFT=0
LIBD="$HOME_DIR/Library"
for sub in "Application Support" Caches Logs Preferences "Saved Application State" HTTPStorages WebKit Cookies LaunchAgents Containers "Group Containers" "Application Scripts" Preferences/ByHost; do
  d="$LIBD/$sub"; [ -d "$d" ] || continue
  while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    NLEFT=$((NLEFT + 1)); L "    $hit ($(du -sk "$hit" 2>/dev/null | awk '{print $1}') KB)"; snap "library: $hit"
  done <<HITS
$(find "$d" -maxdepth 2 \( -iname '*io.redevops.agentic-apps*' -o -iname '*agentic-apps*' -o -iname '*agentic-r0prime*' -o -iname '*Agentic Apps*' -o -iname '*redevops-launcher*' -o -iname '*redevops_launcher*' \) 2>/dev/null)
HITS
done
[ "$NLEFT" -eq 0 ] && rec PASS LB-01 "nothing for $APP_ID / frozen-name agentic-apps / agentic-r0prime / redevops-launcher under ~/Library (Application Support, Caches, Logs, Preferences, Saved Application State, HTTPStorages, WebKit, Cookies, LaunchAgents, Containers)" \
  || rec WARN LB-01 "$NLEFT item(s) under ~/Library; user data left behind is an owner decision after an uninstall"
# An R0-prime run must never create data under the FROZEN identity (docs/R0_PRIME_IDENTITY.md): same name as $FROZEN_APP_ID, with or
# without a suffix such as .plist or .savedState, but NOT $APP_ID (which starts with it).
NFROZ=0
for sub in "Application Support" Caches Logs Preferences "Saved Application State" HTTPStorages WebKit Cookies Containers; do
  d="$LIBD/$sub"; [ -d "$d" ] || continue
  while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    NFROZ=$((NFROZ + 1)); L "    frozen-identity item: $hit"; snap "frozen-identity: $hit"
  done <<HITS
$(find "$d" -maxdepth 2 \( -name "$FROZEN_APP_ID" -o -name "$FROZEN_APP_ID.*" \) ! -name "$APP_ID*" 2>/dev/null)
HITS
done
[ "$NFROZ" -eq 0 ] && rec PASS LB-04 "nothing under the FROZEN bundle id $FROZEN_APP_ID (the R0-prime app keeps its own data, cache and WebKit directories)" \
  || rec FAIL LB-04 "$NFROZ item(s) under the frozen bundle id $FROZEN_APP_ID: created by this run (a defect) or left by a frozen install in this account (then test in a clean account)"
# persistence: a login item or launch agent would be a defect
if [ -d "$LIBD/LaunchAgents" ] && [ -n "$(find "$LIBD/LaunchAgents" -maxdepth 1 \( -iname '*agentic-apps*' -o -iname '*agentic-r0prime*' -o -iname '*Agentic Apps*' -o -iname '*redevops-launcher*' \) 2>/dev/null)" ]; then rec FAIL LB-02 "a LaunchAgent for the app exists (the app must not install persistence)"; else rec PASS LB-02 "no LaunchAgent for the app"; fi
# state directories the product code is known to create (agentic_os/context.py, control_plane.py, mission/onboarding.py, mission/trust.py)
NSTATE=0
for d in "$HOME_DIR/.agentic-os" "$HOME_DIR/.redevops" /.agentic-os; do
  if [ -e "$d" ]; then NSTATE=$((NSTATE + 1)); L "    state dir: $d ($(du -sk "$d" 2>/dev/null | awk '{print $1}') KB)"; snap "state: $d"; fi
done
[ "$NSTATE" -eq 0 ] && rec PASS LB-03 "no ~/.agentic-os, ~/.redevops or /.agentic-os state directory" || rec WARN LB-03 "$NSTATE state dir(s) exist (the sidecar's default AGENTIC_OS_HOME '.agentic-os' is relative to the working directory; a Finder-launched app starts in / )"
# the app bundle itself, for the uninstall step
for a in /Applications/"$APP_NAME" "$HOME_DIR/Applications/$APP_NAME"; do
  [ -e "$a" ] && { L "    app bundle present: $a"; snap "app: $a"; }
done
case "$PHASE" in stopped) :;; esac
L "keychain: not touched (no use of the security tool; ~/Library/Keychains is not opened)"

# ---------------------------------------------------------------- 5. compare with an earlier snapshot
sort -o "$SNAP" "$SNAP"
if [ -n "$COMPARE" ]; then
  if [ -f "$COMPARE" ]; then
    L ""; L "=== difference against $COMPARE ('>' new now, '<' gone)"
    diff <(sort "$COMPARE") "$SNAP" | grep '^[<>]' | sed 's/^/    /' | tee_t
    [ "${PIPESTATUS[0]}" -ge 0 ] || true
  else rec WARN CM-01 "--compare file not found: $COMPARE"; fi
fi

summary
L "snapshot (use it with --compare after the next run): $SNAP"
[ "$N_FAIL" -eq 0 ]
