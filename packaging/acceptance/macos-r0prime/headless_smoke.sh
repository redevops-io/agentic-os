#!/bin/bash
# headless_smoke.sh: run the EMBEDDED sidecar of an Agentic Apps .app with NO GUI and check the local API contract.
#
#   headless_smoke.sh [--out DIR] [--keep] [--start-timeout SECONDS] [--skip-bootstrap] <path to "Agentic Apps.app">
#
# What it does (all under a private work directory created with mktemp -d; nothing outside it is written except the
# transcript under --out):
#   * starts Contents/MacOS/redevops-sidecar in STANDALONE mode (`serve`) under `env -i` with a throwaway HOME, TMPDIR and
#     working directory, bound to 127.0.0.1 on an OS-chosen port (PROJECTS_PORT=0); the one-time launch URL it prints on
#     stderr is the only credential, it is parsed in memory and never written to the transcript;
#   * checks: no session -> 401 on /api/projects and /openapi.json; the UI shell loads with the capability shim; Host
#     rebinding and cross-origin requests are refused with no CORS headers; the launch code is exchanged for a session token
#     in the response body (NO cookie: the credential is the X-Agentic-Session header, state changes also need
#     X-Agentic-CSRF); /api/projects is 200 with the header; /openapi.json is 200 with the header; the code is single use;
#     a session without the CSRF header cannot DELETE; logout revokes the session; a Cookie header alone is refused;
#   * runs `redevops-sidecar bootstrap` with stdout redirected, under a UTF-8 and an ISO8859-1 locale (guards the v0.3.2
#     UnicodeEncodeError defect: the verdict must be rc 0/1 with the same bytes in both);
#   * stops the sidecar BY EXACT PID (SIGTERM to the bootloader, which forwards it; SIGKILL by exact PID only as a
#     recorded fallback), then proves: no process, no listener, no _MEI directory, and lists what the run wrote into the
#     throwaway HOME and working directory;
#   * removes its work directory (find -delete, only when the path is the mktemp result) unless --keep.
# It never runs the launcher, never opens a window or a browser, never calls sudo, signs nothing, and does not touch
# the keychain. It does not replace the GUI checklist: it cannot say anything about the Tauri window or Gatekeeper.
# Exit status: 0 no FAIL, 1 at least one FAIL, 2 usage/precondition error.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"

OUT=""; KEEP=0; START_TIMEOUT=120; SKIP_BOOT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="${2:?}"; shift 2;;
    --keep) KEEP=1; shift;;
    --start-timeout) START_TIMEOUT="${2:?}"; shift 2;;
    --skip-bootstrap) SKIP_BOOT=1; shift;;
    -h|--help) sed -n '2,/^set -u/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0;;
    -*) die "unknown option: $1";;
    *) break;;
  esac
done
[ $# -eq 1 ] || die "usage: headless_smoke.sh [options] <path to .app>   (see --help)"
[ "$(uname -s)" = Darwin ] || die "macOS only"
[ "$(id -u)" -ne 0 ] || die "do not run as root"
APP_IN="$1"; [ -d "$APP_IN" ] || die "not a directory: $APP_IN"
APP="$(abs_path "$APP_IN")"
SIDECAR="$APP/Contents/MacOS/redevops-sidecar"
[ -x "$SIDECAR" ] || die "sidecar not found or not executable: $SIDECAR"
case "$(file -b "$SIDECAR")" in Mach-O*arm64*) ;; *) die "sidecar is not an arm64 Mach-O: $(file -b "$SIDECAR")";; esac
[ "$(uname -m)" = arm64 ] || die "this candidate is arm64 only; run it on an Apple Silicon Mac"

[ -n "$OUT" ] || OUT="$PWD/r0p-out/smoke-$(utc_stamp)"
if [ -e "$OUT" ]; then [ -d "$OUT" ] && [ -z "$(ls -A "$OUT")" ] || die "--out exists and is not empty: $OUT"; fi
mkdir -p "$OUT" || die "cannot create $OUT"
OUT="$(abs_path "$OUT")"
TRANSCRIPT="$OUT/smoke-transcript.txt"; : >"$TRANSCRIPT"

W="$(mktemp -d "${TMPDIR:-/tmp}/r0p-smoke.XXXXXX")" || die "mktemp failed"
W="$(cd "$W" && pwd -P)"
mkdir -p "$W/home" "$W/tmp" "$W/cwd" "$W/http" "$W/boot"
PID=""; CHILDREN=""; PORT=""; CODE=""
SIDECAR_ENV_BASE=(PATH=/usr/bin:/bin "HOME=$W/home" "TMPDIR=$W/tmp" LANG=en_US.UTF-8)

emergency() { # exact PIDs only; the descendants are collected BEFORE the parent dies (a killed bootloader orphans its child)
  local p
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then CHILDREN="$(descendants "$PID" | tr '\n' ' ') $CHILDREN"; fi
  for p in $CHILDREN $PID; do
    [ -n "$p" ] && kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null
  done
}
remove_work() {
  [ "$KEEP" = 1 ] && { L "work dir kept: $W"; return 0; }
  case "$W" in "${TMPDIR:-/tmp}"/r0p-smoke.*|/tmp/r0p-smoke.*|/private/tmp/r0p-smoke.*|/private/var/folders/*/r0p-smoke.*)
    if [ -d "$W" ] && [ ! -L "$W" ]; then find "$W" -depth -delete 2>/dev/null; fi;; *) L "work dir not removed (unexpected path): $W"; return 1;; esac
  [ -e "$W" ] && L "work dir could not be removed completely: $W" || L "work dir removed"
}
cleanup() {
  local rc=$?
  set +e
  # the normal path already stopped the sidecar; this handles an abort or a failed start
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    L "cleanup: stopping the sidecar (pid $PID) after an early exit"
    CHILDREN="$(descendants "$PID" | tr '\n' ' ') $CHILDREN"
    kill -TERM "$PID" 2>/dev/null; sleep 3; emergency
  fi
  [ -n "$W" ] && [ -d "$W" ] && remove_work
  exit "$rc"
}
trap cleanup EXIT INT TERM

header "headless_smoke"
L "app: $APP"
L "sidecar: $SIDECAR sha256=$(sha256_of "$SIDECAR")"
L "work dir (throwaway HOME, TMPDIR, cwd): $W"
L "environment of the sidecar: env -i ${SIDECAR_ENV_BASE[*]} PROJECTS_HOST=127.0.0.1 PROJECTS_PORT=0"
L ""

# http NAME METHOD PATH [extra curl args...]: sets H_CODE; headers in $W/http/NAME.hdr, body in $W/http/NAME.body
H_CODE=""
http() {
  local name="$1" method="$2" path="$3"; shift 3
  H_CODE="$(curl -sS --noproxy '*' -m 15 -X "$method" -o "$W/http/$name.body" -D "$W/http/$name.hdr" -w '%{http_code}' "$@" "http://127.0.0.1:$PORT$path" 2>"$W/http/$name.err")" || H_CODE="000"
}
has_hdr() { grep -qi "^$2:" "$W/http/$1.hdr" 2>/dev/null; }
hdr_val() { grep -i "^$2:" "$W/http/$1.hdr" 2>/dev/null | head -1 | sed 's/^[^:]*: *//' | tr -d '\r'; }
descendants() { # pid -> pids of all descendants
  local p c
  for c in $(pgrep -P "$1" 2>/dev/null); do echo "$c"; descendants "$c"; done
}

# ---------------------------------------------------------------- start
( cd "$W/cwd" && exec env -i "${SIDECAR_ENV_BASE[@]}" PROJECTS_HOST=127.0.0.1 PROJECTS_PORT=0 "$SIDECAR" serve ) >"$W/stdout.txt" 2>"$W/stderr.txt" </dev/null &
PID=$!
L "started sidecar bootloader pid=$PID (standalone serve, stdin from /dev/null)"
URL=""; t0=$(date +%s)
while :; do
  URL="$(grep -o 'http://127\.0\.0\.1:[0-9]*/#lc=[A-Za-z0-9_-]*' "$W/stderr.txt" 2>/dev/null | tail -1)"
  [ -n "$URL" ] && break
  kill -0 "$PID" 2>/dev/null || break
  [ $(( $(date +%s) - t0 )) -ge "$START_TIMEOUT" ] && break
  sleep 0.5
done
STARTED_IN=$(( $(date +%s) - t0 ))
if [ -z "$URL" ]; then
  rec FAIL SM-01 "no one-time launch URL on stderr within ${START_TIMEOUT}s (process alive: $(kill -0 "$PID" 2>/dev/null && echo yes || echo no)); stderr tail below"
  tail -15 "$W/stderr.txt" | sed 's/#lc=[A-Za-z0-9_-]*/#lc=<masked>/' | sed 's/^/    /' | tee_t
  emergency; summary; exit 1
fi
PORT="$(printf '%s' "$URL" | sed -n 's#^http://127\.0\.0\.1:\([0-9]*\)/.*#\1#p')"
CODE="${URL##*#lc=}"
rec PASS SM-01 "sidecar printed a one-time launch URL for 127.0.0.1:$PORT after ${STARTED_IN}s (code length ${#CODE}, not recorded)"
[ "${#CODE}" -ge 16 ] && rec PASS SM-01b "launch code length ${#CODE} >= 16" || rec FAIL SM-01b "launch code is only ${#CODE} characters"
CHILDREN="$(descendants "$PID" | tr '\n' ' ')"
L "process tree: bootloader $PID, descendants: ${CHILDREN:-none}"
[ -n "$CHILDREN" ] && rec PASS SM-02 "onefile sidecar is a bootloader plus a Python child (expected two processes)" || rec WARN SM-02 "no child process found under the bootloader (layout changed?)"

LS="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1')"
L "listener(s) on $PORT:"; printf '%s\n' "$LS" | sed 's/^/    /' | tee_t
LPID="$(printf '%s\n' "$LS" | awk 'NR==1{print $2}')"
case " $PID $CHILDREN " in *" $LPID "*) rec PASS SM-03 "the listener belongs to the sidecar tree (pid $LPID)";; *) rec FAIL SM-03 "the listener pid '$LPID' is not in the sidecar tree";; esac
printf '%s\n' "$LS" | grep -q "127\.0\.0\.1:$PORT" && ! printf '%s\n' "$LS" | grep -qE '(\*|0\.0\.0\.0|\[::\]):'"$PORT" && rec PASS SM-04 "bound to 127.0.0.1 only" || rec FAIL SM-04 "not bound to 127.0.0.1 only"

# ---------------------------------------------------------------- unauthenticated behaviour
L ""; L "=== without a session"
http a1 GET /api/projects
[ "$H_CODE" = 401 ] && rec PASS SM-10 "GET /api/projects without a session -> 401" || rec FAIL SM-10 "GET /api/projects without a session -> $H_CODE (expected 401)"
http a2 GET /openapi.json
[ "$H_CODE" = 401 ] && rec PASS SM-11 "GET /openapi.json without a session -> 401" || rec FAIL SM-11 "GET /openapi.json without a session -> $H_CODE (expected 401)"
http a3 GET /
if [ "$H_CODE" = 200 ] && grep -q 'local-capability-shim.js' "$W/http/a3.body"; then rec PASS SM-12 "UI shell served (200) with the capability shim tag"; else rec FAIL SM-12 "GET / -> $H_CODE or no shim tag"; fi
http a4 GET /local-capability-shim.js
[ "$H_CODE" = 200 ] && rec PASS SM-13 "shim script served (200)" || rec FAIL SM-13 "shim script -> $H_CODE"
http a5 GET /api/local/session
[ "$H_CODE" = 401 ] && rec PASS SM-14 "GET /api/local/session without a session header -> 401" || rec FAIL SM-14 "GET /api/local/session without a session header -> $H_CODE"
http a6 GET /api/projects -H 'Host: evil.example'
case "$H_CODE" in 400|403|421) rec PASS SM-15 "foreign Host header refused ($H_CODE)";; *) rec FAIL SM-15 "foreign Host header -> $H_CODE (expected 400/403/421)";; esac
http a7 OPTIONS /api/sources/confirm -H 'Origin: https://evil.example' -H 'Access-Control-Request-Method: POST' -H 'Access-Control-Request-Headers: content-type'
if [ "$H_CODE" = 403 ] && ! has_hdr a7 access-control-allow-origin; then rec PASS SM-16 "cross-origin preflight -> 403, no Access-Control-Allow-Origin"; else rec FAIL SM-16 "cross-origin preflight -> $H_CODE, ACAO present: $(has_hdr a7 access-control-allow-origin && echo yes || echo no)"; fi
http a8 POST /api/sources/confirm -H 'Origin: https://evil.example' -H 'Content-Type: text/plain' --data '{"folder":"/"}'
case "$H_CODE" in 4??) rec PASS SM-17 "cross-origin text/plain POST refused ($H_CODE)";; *) rec FAIL SM-17 "cross-origin text/plain POST -> $H_CODE (the v0.3.2 exposure)";; esac
http a9 GET / ; has_hdr a9 access-control-allow-origin && rec FAIL SM-18 "Access-Control-Allow-Origin present on a normal response" || rec PASS SM-18 "no Access-Control-Allow-Origin on a normal response"
has_hdr a9 content-security-policy && rec PASS SM-19 "Content-Security-Policy header present ($(hdr_val a9 content-security-policy | cut -c1-60)...)" || rec WARN SM-19 "no Content-Security-Policy header on /"

# ---------------------------------------------------------------- session exchange
L ""; L "=== launch code -> session"
http s1 POST /api/local/session -H 'Content-Type: application/json' -H 'Accept: application/json' --data "{\"code\":\"$CODE\"}"
if [ "$H_CODE" = 200 ]; then rec PASS SM-20 "POST /api/local/session with the launch code -> 200"; else rec FAIL SM-20 "session exchange -> $H_CODE"; fi
has_hdr s1 set-cookie && rec FAIL SM-21 "the exchange response sets a cookie (the header-session design sets none)" || rec PASS SM-21 "no Set-Cookie on the exchange response (header session, not a cookie)"
TOK="$(sed -n 's/.*"session_token" *: *"\([^"]*\)".*/\1/p' "$W/http/s1.body" | head -1)"
SHDR="$(sed -n 's/.*"session_header" *: *"\([^"]*\)".*/\1/p' "$W/http/s1.body" | head -1)"
[ -n "$TOK" ] && rec PASS SM-22 "response carries a session_token (${#TOK} chars, not recorded)" || rec FAIL SM-22 "no session_token in the exchange response"
[ "$SHDR" = "X-Agentic-Session" ] && rec PASS SM-23 "session header name is X-Agentic-Session" || rec FAIL SM-23 "unexpected session header name '$SHDR'"
CSRF="$(sed -n 's/.*"csrf_token" *: *"\([^"]*\)".*/\1/p' "$W/http/s1.body" | head -1)"
[ -n "$CSRF" ] && rec PASS SM-26 "response carries a csrf_token (${#CSRF} chars, not recorded)" || rec FAIL SM-26 "no csrf_token in the exchange response"
http s2 GET /api/projects -H "X-Agentic-Session: $TOK"
if [ "$H_CODE" = 200 ]; then rec PASS SM-27 "GET /api/projects with the session header -> 200 ($(wc -c <"$W/http/s2.body" | tr -d ' ') bytes; sample project listed: $(grep -c 'customer-ops' "$W/http/s2.body" | tr -d ' ') mention(s) of customer-ops)"; else rec FAIL SM-27 "GET /api/projects with the session header -> $H_CODE"; fi
http s3 GET /openapi.json -H "X-Agentic-Session: $TOK"
[ "$H_CODE" = 200 ] && rec PASS SM-28 "GET /openapi.json with the session header -> 200 (v0.3.2 returned 500)" || rec FAIL SM-28 "GET /openapi.json with the session header -> $H_CODE"
http s4 GET /api/local/session -H "X-Agentic-Session: $TOK" -H 'Accept: application/json'
[ "$H_CODE" = 200 ] && grep -q csrf_token "$W/http/s4.body" && rec PASS SM-29 "reload path: GET /api/local/session with the session header returns a csrf_token" || rec FAIL SM-29 "reload path -> $H_CODE"
http s4b GET /api/projects -H "Cookie: agentic_os_session=$TOK"
[ "$H_CODE" = 401 ] && rec PASS SM-29b "the token sent as a Cookie header is not a credential (401)" || rec FAIL SM-29b "a Cookie header was accepted ($H_CODE)"
http s5 POST /api/local/session -H 'Content-Type: application/json' --data "{\"code\":\"$CODE\"}"
[ "$H_CODE" = 401 ] && rec PASS SM-30 "the launch code is single use (replay -> 401)" || rec FAIL SM-30 "replay of the launch code -> $H_CODE (expected 401)"
http s6 DELETE /api/local/session -H "X-Agentic-Session: $TOK"
[ "$H_CODE" = 403 ] && rec PASS SM-31 "DELETE with the session header but without the CSRF header -> 403" || rec FAIL SM-31 "DELETE without CSRF -> $H_CODE (expected 403)"
http s7 DELETE /api/local/session -H "X-Agentic-Session: $TOK" -H "X-Agentic-CSRF: $CSRF"
[ "$H_CODE" = 200 ] && rec PASS SM-32 "logout with the CSRF header -> 200" || rec FAIL SM-32 "logout -> $H_CODE"
http s8 GET /api/projects -H "X-Agentic-Session: $TOK"
[ "$H_CODE" = 401 ] && rec PASS SM-33 "after logout the old session token is refused (401)" || rec FAIL SM-33 "after logout /api/projects -> $H_CODE (expected 401)"
CODE=""; CSRF=""; TOK=""

# ---------------------------------------------------------------- bootstrap under two locales
L ""; L "=== bootstrap with stdout redirected (v0.3.2 defect a: UnicodeEncodeError)"
if [ "$SKIP_BOOT" = 1 ]; then rec SKIP SM-40 "bootstrap skipped (--skip-bootstrap)"; else
  for loc in en_US.UTF-8 en_US.ISO8859-1; do
    o="$W/boot/out-$loc.txt"; e="$W/boot/err-$loc.txt"
    ( cd "$W/cwd" && exec env -i PATH=/usr/bin:/bin "HOME=$W/home" "TMPDIR=$W/tmp" "LC_ALL=$loc" RDO_DISABLE_PAIR=1 "$SIDECAR" bootstrap ) >"$o" 2>"$e" </dev/null &
    bp=$!; t0=$(date +%s)
    while kill -0 "$bp" 2>/dev/null; do [ $(( $(date +%s) - t0 )) -ge 90 ] && { kill -KILL "$bp" 2>/dev/null; L "    bootstrap pid $bp exceeded 90 s and was killed by exact pid"; break; }; sleep 0.5; done
    wait "$bp" 2>/dev/null; brc=$?
    L "    bootstrap [$loc]: exit $brc, $(wc -c <"$o" | tr -d ' ') bytes stdout, $(wc -c <"$e" | tr -d ' ') bytes stderr"
    grep -q 'UnicodeEncodeError' "$e" && rec FAIL SM-40 "bootstrap under $loc raised UnicodeEncodeError" || rec PASS SM-40 "bootstrap under $loc: no UnicodeEncodeError"
    case "$brc" in 0|1) rec PASS SM-41 "bootstrap under $loc: exit $brc (0 = ready, 1 = blocked verdict; both are verdicts)";; *) rec FAIL SM-41 "bootstrap under $loc: exit $brc (crash?)";; esac
    eval "BOOT_$(printf '%s' "$loc" | tr -c 'A-Za-z0-9' _)=$(wc -c <"$o" | tr -d ' ')"
  done
  [ "${BOOT_en_US_UTF_8:-x}" = "${BOOT_en_US_ISO8859_1:-y}" ] && rec PASS SM-42 "bootstrap output byte count identical under both locales" || rec FAIL SM-42 "bootstrap output differs between locales (UTF-8: ${BOOT_en_US_UTF_8:-?}, ISO8859-1: ${BOOT_en_US_ISO8859_1:-?})"
fi

# ---------------------------------------------------------------- stop by exact PID and prove nothing is left
L ""; L "=== stop by exact PID"
CHILDREN="$(descendants "$PID" | tr '\n' ' ') $CHILDREN"
t0=$(date +%s); kill -TERM "$PID" 2>/dev/null
while kill -0 "$PID" 2>/dev/null && [ $(( $(date +%s) - t0 )) -lt 20 ]; do sleep 0.3; done
if kill -0 "$PID" 2>/dev/null; then rec WARN SM-50 "the bootloader ignored SIGTERM for 20 s: killing by exact pid"; emergency; sleep 1; else rec PASS SM-50 "SIGTERM to the bootloader pid ended it in $(( $(date +%s) - t0 )) s"; fi
wait "$PID" 2>/dev/null; src=$?
L "    bootloader exit status $src"
LEFT=""; for p in $CHILDREN; do kill -0 "$p" 2>/dev/null && LEFT="$LEFT $p"; done
if [ -z "$LEFT" ]; then rec PASS SM-51 "no process of the sidecar tree is left"; else rec FAIL SM-51 "left running:$LEFT (killing by exact pid)"; for p in $LEFT; do kill -KILL "$p" 2>/dev/null; done; fi
PID_GONE="$PID"; PID=""
lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && rec FAIL SM-52 "port $PORT still has a listener" || rec PASS SM-52 "no listener on port $PORT"
MEI="$(find "$W/tmp" -maxdepth 1 -name '_MEI*' 2>/dev/null | wc -l | tr -d ' ')"
[ "$MEI" = 0 ] && rec PASS SM-53 "no _MEI* directory left in the sidecar's TMPDIR" || rec FAIL SM-53 "$MEI _MEI* directory(ies) left in the sidecar's TMPDIR"
L "files written by the run into the throwaway HOME ($(find "$W/home" -type f | wc -l | tr -d ' ')):"; ( cd "$W/home" && find . -type f | sort | head -40 ) | sed 's/^/    /' | tee_t
L "files written into the working directory ($(find "$W/cwd" -type f | wc -l | tr -d ' ')):"; ( cd "$W/cwd" && find . -type f | sort | head -40 ) | sed 's/^/    /' | tee_t
[ -z "$(find "$W/cwd" -type f | head -1)" ] && rec INFO SM-54 "nothing written into the working directory" || rec WARN SM-54 "the sidecar wrote files into its working directory (a Finder-launched app starts in /): see the list above"
grep -q '#lc=' "$W/stdout.txt" && rec FAIL SM-55 "a launch code reached stdout" || rec PASS SM-55 "no launch code on stdout"
cp "$W/stdout.txt" "$OUT/sidecar-stdout.txt"
sed 's/#lc=[A-Za-z0-9_-]*/#lc=<masked>/g' "$W/stderr.txt" >"$OUT/sidecar-stderr.masked.txt"
L "sidecar stdout/stderr (stderr masked) saved under $OUT"

# ---------------------------------------------------------------- remove the work directory
remove_work
summary
L "transcript: $TRANSCRIPT"
[ "$N_FAIL" -eq 0 ]
