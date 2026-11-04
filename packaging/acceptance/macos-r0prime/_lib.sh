#!/bin/bash
# Shared helpers for the R0-prime macOS acceptance kit. Sourced, not executed.
# Rules for every script of the kit: bash 3.2 compatible (the macOS /bin/bash), ASCII only, no sudo, no keychain
# access (the `security` tool is never called), no change to any system setting, no GUI (no `open`), no rm -rf.
# shellcheck shell=bash

# Never let the caller's credentials or agent leak into a child.
unset SSH_AUTH_SOCK SSH_AGENT_PID APPROVAL_SECRET 2>/dev/null || true
export LC_ALL=C

KIT_NAME="r0prime-macos-kit"
APP_ID="io.redevops.agentic-apps"
TRANSCRIPT=""
N_PASS=0; N_FAIL=0; N_WARN=0; N_INFO=0; N_SKIP=0
RESULT_ROWS=""

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
utc_stamp() { date -u +%Y%m%dT%H%M%SZ; }

die() { echo "ERROR: $*" >&2; exit 2; }

# L: print a line to the terminal and the transcript.
L() {
  printf '%s\n' "$*"
  if [ -n "$TRANSCRIPT" ]; then printf '%s\n' "$*" >>"$TRANSCRIPT"; fi
}

# rec STATUS ID TEXT: one result row. Statuses: PASS FAIL WARN INFO SKIP.
rec() {
  local st="$1" id="$2"; shift 2
  case "$st" in
    PASS) N_PASS=$((N_PASS + 1));; FAIL) N_FAIL=$((N_FAIL + 1));; WARN) N_WARN=$((N_WARN + 1));;
    INFO) N_INFO=$((N_INFO + 1));; SKIP) N_SKIP=$((N_SKIP + 1));;
  esac
  L "[$st] $id: $*"
  RESULT_ROWS="${RESULT_ROWS}${st}	${id}	$*
"
}

# cap LABEL CMD...: run a command, log "$ cmd" and its combined output (indented), keep CAP_OUT and CAP_RC.
cap() {
  local label="$1"; shift
  CAP_RC=0
  CAP_OUT="$("$@" 2>&1)" || CAP_RC=$?
  L "--- $label (exit $CAP_RC)"
  if [ -n "$CAP_OUT" ]; then printf '%s\n' "$CAP_OUT" | sed 's/^/    /' | tee_t; fi
}
tee_t() { if [ -n "$TRANSCRIPT" ]; then tee -a "$TRANSCRIPT"; else cat; fi; }

sha256_of() { shasum -a 256 "$1" | awk '{print $1}'; }

lower() { printf '%s' "$1" | tr 'A-Z' 'a-z'; }

# Absolute, symlink-free path of an existing file or directory.
abs_path() {
  if [ -d "$1" ]; then ( cd "$1" && pwd -P ); else ( cd "$(dirname "$1")" && printf '%s/%s\n' "$(pwd -P)" "$(basename "$1")" ); fi
}

# vge A B: success when dotted version A >= B (numeric components, missing = 0).
vge() {
  awk -v a="$1" -v b="$2" 'BEGIN{
    na=split(a,x,"."); nb=split(b,y,"."); n=(na>nb)?na:nb;
    for(i=1;i<=n;i++){ xi=(i<=na)?x[i]+0:0; yi=(i<=nb)?y[i]+0:0;
      if(xi>yi) exit 0; if(xi<yi) exit 1 }
    exit 0 }'
}

# header TITLE: standard transcript header (host facts that are safe to record).
header() {
  L "$KIT_NAME: $1"
  L "time (UTC): $(utc_now)"
  L "host: $(hostname) / $(sw_vers -productName) $(sw_vers -productVersion) build $(sw_vers -buildVersion) / $(uname -m) / kernel $(uname -r)"
  L "user: $(id -un) uid=$(id -u) gid=$(id -g); effective root: $([ "$(id -u)" -eq 0 ] && echo YES || echo no)"
  L "kit script: $(basename "$0") sha256=$(sha256_of "$0")"
  L ""
}

# have_clt: true when real developer tools are present (without Xcode/CLT the /usr/bin shims for xcrun, vtool, otool
# would pop up an install dialog: never call them in that case).
have_clt() { xcode-select -p >/dev/null 2>&1; }

summary() {
  L ""
  L "SUMMARY: PASS=$N_PASS FAIL=$N_FAIL WARN=$N_WARN INFO=$N_INFO SKIP=$N_SKIP"
  if [ -n "$TRANSCRIPT" ]; then
    printf '%s' "$RESULT_ROWS" >"${TRANSCRIPT%.txt}.results.tsv"
  fi
}

# Remove an empty-or-not directory tree that this script created, file by file (never rm -rf). Refuses anything that
# is not a real directory strictly inside the given root.
purge_tree() { # root dir
  local root="$1" d="$2"
  [ -d "$d" ] && [ ! -L "$d" ] || return 0
  case "$d" in "$root"/?*) ;; *) echo "purge_tree: refusing $d (not inside $root)" >&2; return 1;; esac
  find "$d" -depth \( -type f -o -type l \) -exec rm -f {} + 2>/dev/null
  find "$d" -depth -type d -exec rmdir {} + 2>/dev/null
  [ ! -e "$d" ]
}
