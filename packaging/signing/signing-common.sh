# signing-common.sh  (version r0prime-common-3)  -- SOURCED by the R0-prime signing scripts; it is not run on its own.
# Shared safety functions so that the three scripts (macOS, Windows, SSHSIG) behave the same way. Its sha256 is bound by every
# scope file (`common_sha256`), like the script's own. bash 3.2 compatible. No `set -x`, no rm -r, no secrets.
#
# The caller defines before use: SELF (absolute path of the calling script), SELF_DIR, and a `die` function that exits.
#
# What is here, and the review finding it answers (round 4):
#   r0p_scrub        R4-05  the environment must BE the allowlist; a pre-set marker variable cannot skip the scrub (the check looks at the
#                           environment itself); one re-exec at most, a second failure is fatal
#   r0p_fix_path     R4-05  fixed system PATH (plus an explicit, validated owner tools directory); stubs only with R0P_ALLOW_NONSYSTEM_TOOLS=stub-test
#   r0p_mktmp        R4-03  a private 0700 temp directory owned by the caller
#   r0p_scope_load   R4-03  the scope file is read ONCE into a private copy, the copy is hashed, the copy is parsed
#   r0p_window_check R4-04  UTC not-before/not-after window, at most 24 hours
#   r0p_ledger_*     R4-04  one-time ledger: a scope hash that was used is refused again unless --allow-reuse is given
#   r0p_gate         R4-09  distribution gate: bound to the repository copy by hash, printed, enforced with --distribution

r0p_sha() { /usr/bin/shasum -a 256 "$1" | /usr/bin/awk '{print $1}'; }
r0p_sha_str() { printf '%s' "$1" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}'; }

# r0p_scrub "<space separated extra variable names that may be present>"
# VERIFICATION, not re-execution (round 5): each script re-executes itself unconditionally in its first lines (bash -p, /usr/bin/env -i, fixed PATH,
# fds 3-9 closed) before any function or PATH-resolved command runs. Here the running copy checks that it really is that copy: privileged mode, the
# re-exec marker, an environment that is exactly the allowlist, no BASH_FUNC_ entry, and it closes every other inherited file descriptor. Anything else
# is fatal (there is no flag that skips this). Builtins are called with `builtin` and externals by absolute path.
R0P_BASE_ALLOW=" PATH HOME LANG LC_ALL TERM PWD OLDPWD SHLVL _ R0P_REEXEC "
R0P_FIXED_PATH="/usr/bin:/bin:/usr/sbin:/sbin"
R0P_TMP=""
r0p_scrub() {
  local extra=" $1 " v bad="" allow i
  allow="$R0P_BASE_ALLOW${extra# }"
  [[ "$-" == *p* && "${R0P_REEXEC:-}" == 1 ]] || { echo "ERROR: the script is not the re-executed privileged-mode copy; refusing" >&2; exit 1; }
  for v in $(builtin compgen -A export); do
    case "$allow" in *" $v "*) ;; *) bad="$bad $v";; esac
  done
  if /usr/bin/env | /usr/bin/grep -q '^BASH_FUNC_'; then bad="$bad <exported-function>"; fi
  if [ -n "$bad" ]; then echo "ERROR: the environment is not the allowlist (extra:$bad); refusing" >&2; exit 1; fi
  # file descriptors above 9 (3 to 9 were closed by the re-exec); 255 is bash's own script descriptor and is left alone
  for ((i = 10; i < 64; i++)); do eval "exec $i>&-"; done
}

# r0p_fix_path [extra-dirs]  : PATH becomes the fixed system path (+ the validated extra directories). R0P_ALLOW_NONSYSTEM_TOOLS=stub-test
# keeps the inherited PATH (unit tests only) and is REFUSED in --real (r0p_no_test_switch).
R0P_STUBS=0
r0p_fix_path() {
  local extra="${1:-}" d
  if [ "${R0P_ALLOW_NONSYSTEM_TOOLS:-}" = "stub-test" ]; then R0P_STUBS=1; return 0; fi
  if [ -n "$extra" ]; then
    local IFS=:
    for d in $extra; do
      case "$d" in /*) ;; *) die "tools directory must be an absolute path: $d";; esac
      [ -d "$d" ] && [ ! -L "$d" ] || die "tools directory is not a real directory: $d"
      # owned by the caller (or root) and not writable by group or others
      local owner perm
      owner="$(/usr/bin/stat -f %u "$d")"; perm="$(/usr/bin/stat -f %Lp "$d")"
      { [ "$owner" = "$(/usr/bin/id -u)" ] || [ "$owner" = 0 ]; } || die "tools directory $d is owned by someone else"
      case "${perm: -2}" in [0145][0145]) ;; *) die "tools directory $d is writable by group or others (mode $perm)";; esac
    done
  fi
  PATH="$R0P_FIXED_PATH${extra:+:$extra}"; export PATH
}
# The unit-test switch must never be honoured when a real identity, agent or service can be reached (round 5, R5-02). Called by each script as soon
# as the mode is known. (The unit tests run a copy of the library in which the next line is replaced by a no-op; that copy has another sha256.)
r0p_no_test_switch() { if [ -n "${R0P_ALLOW_NONSYSTEM_TOOLS:-}" ]; then die "R0P_ALLOW_NONSYSTEM_TOOLS (a unit-test switch) is refused in --real"; fi; }   # R0P-TEST-PATCH-POINT

# private temp directory (0700) for scope copies and the like; removed by r0p_tmp_cleanup
r0p_mktmp() {
  R0P_TMP="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/r0p.XXXXXXXX")" || die "cannot create a private temp directory"
  chmod 700 "$R0P_TMP"
}
r0p_tmp_cleanup() {
  local p
  if [ -n "${R0P_TMP:-}" ] && [ -d "$R0P_TMP" ]; then
    /usr/bin/find "$R0P_TMP" -depth -type f -print0 | while IFS= read -r -d '' p; do : > "$p" 2>/dev/null; rm -f "$p"; done
    /usr/bin/find "$R0P_TMP" -depth -type d -print0 | while IFS= read -r -d '' p; do rmdir "$p" 2>/dev/null || true; done
  fi
  return 0
}

# r0p_scope_load FILE EXPECTED_SHA KEY...   -> variables S_<key>; dies on any problem.
# The file is read once into $R0P_TMP/scope.copy (a file only this user can touch); the hash is taken of THAT copy and the copy is
# parsed, so what is parsed is exactly what was hashed. KEY=VALUE lines only, '#' comments, unknown or duplicate keys refused.
r0p_scope_load() {
  local f="$1" want="$2" keys=" ${*:3} " k line key val seen=" " c
  [ -n "$R0P_TMP" ] || r0p_mktmp
  c="$R0P_TMP/scope.copy"
  [ -f "$f" ] && [ ! -L "$f" ] || die "the scope file must be a regular file (not a symlink)"
  cat "$f" > "$c" || die "cannot read the scope file"
  chmod 600 "$c"
  [ "$(r0p_sha "$c")" = "$want" ] || die "the scope file's sha256 does not equal the value given to --i-have-owner-authorization"
  for k in ${*:3}; do eval "S_$k="; done
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue;; esac
    key="${line%%=*}"; val="${line#*=}"
    [ "$key" != "$line" ] || die "scope file: line without '='"
    case "$val" in *[[:cntrl:]]*) die "scope file: control character in a value";; esac
    case "$seen" in *" $key "*) die "scope file: duplicate key $key";; esac
    seen="$seen$key "
    case "$keys" in *" $key "*) eval "S_$key=\$val";; *) die "scope file: unknown key '$key'";; esac
  done < "$c"
  for k in ${*:3}; do
    case "$seen" in *" $k "*) ;; *) die "scope file: required key '$k' is missing";; esac
  done
}

# r0p_epoch 'YYYY-MM-DDTHH:MM:SSZ' -> seconds since the epoch (UTC)
r0p_epoch() {
  [[ "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || return 1
  /bin/date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null
}
# r0p_window_check NOT_BEFORE NOT_AFTER : now must be inside; the window is at most 24 hours (hard limit)
R0P_MAX_WINDOW=86400
r0p_window_check() {
  local a b now
  a="$(r0p_epoch "$1")" || die "scope file: not_before must be YYYY-MM-DDTHH:MM:SSZ (UTC)"
  b="$(r0p_epoch "$2")" || die "scope file: not_after must be YYYY-MM-DDTHH:MM:SSZ (UTC)"
  [ "$b" -gt "$a" ] || die "scope file: not_after is not after not_before"
  [ $((b - a)) -le "$R0P_MAX_WINDOW" ] || die "scope file: the authorized window is longer than 24 hours (hard limit)"
  now="$(/bin/date -u +%s)"
  { [ "$now" -ge "$a" ] && [ "$now" -le "$b" ]; } || die "scope file: now (UTC $(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)) is outside the authorized window $1 .. $2"
}

# one-time ledger (round 5, R5-04) ----------------------------------------------------------------------------------------
# --ledger DIR is a DIRECTORY the owner keeps outside the repository:
#   events/NNNNNN.txt  one line per event, created with O_EXCL (noclobber) so a planted symlink or file is never followed or overwritten;
#                      each line carries the sha256 of the previous event file (a hash chain)
#   claims/<scope sha> one directory per scope file, created with mkdir (atomic): the first run that claims a scope wins, a parallel run loses
#   LENGTH             "<number of events> <sha256 of the last event file>", rewritten after each event (detects deletion of the last events)
#   lock/              mkdir lock held while events are appended
# The chain and LENGTH make tampering with the middle or the tail DETECTABLE; they cannot stop the owner's own account from deleting the whole
# directory (documented). The scope is CLAIMED (burned) right before the first use of the owner's session and stays claimed if the run fails.
R0P_ZERO64="$(printf '%064d' 0)"      # the "previous hash" of the first event
r0p_ledger_prepare() {
  local l="$1" perm owner s
  [ -n "$l" ] || die "--real needs --ledger DIR (a directory outside the repository that records used scope files)"
  case "$l" in /*) ;; *) die "--ledger must be an absolute path";; esac
  [ -d "$(dirname "$l")" ] || die "the parent directory of --ledger does not exist"
  [ ! -L "$l" ] || die "--ledger must not be a symlink"
  if /bin/mkdir "$l" 2>/dev/null; then
    # created by this run: build the layout, LENGTH LAST (parallel runs wait for it)
    /bin/mkdir "$l/events" "$l/claims" || die "cannot create the ledger directory"
    chmod 700 "$l" "$l/events" "$l/claims"
    ( set -C; printf '0 %s\n' "$R0P_ZERO64" > "$l/LENGTH" ) || die "cannot create LENGTH"
  else
    [ -d "$l" ] || die "--ledger must be a directory (an older single-file ledger is not accepted: use a new directory)"
    local i
    for ((i = 0; i < 50; i++)); do [ -f "$l/LENGTH" ] && break; /bin/sleep 0.1; done      # another run may be creating it right now
    owner="$(/usr/bin/stat -f %u "$l")"; perm="$(/usr/bin/stat -f %Lp "$l")"
    [ "$owner" = "$(/usr/bin/id -u)" ] || die "--ledger is owned by someone else"
    case "${perm: -2}" in [0145][0145]) ;; *) die "--ledger is writable by group or others (mode $perm)";; esac
    for s in events claims; do [ -d "$l/$s" ] && [ ! -L "$l/$s" ] || die "--ledger: $s is missing or a symlink"; done
  fi
}
# r0p_ledger_verify LEDGER : chain intact, numbering contiguous, not shorter than the recorded length; prints nothing; sets R0P_LEDGER_N / R0P_LEDGER_LAST
R0P_LEDGER_N=0; R0P_LEDGER_LAST="$R0P_ZERO64"
r0p_ledger_verify() {
  local l="$1" n=0 prev="$R0P_ZERO64" f rec_n rec_h line
  read -r rec_n rec_h < "$l/LENGTH" 2>/dev/null || die "ledger: LENGTH is missing or unreadable"
  [[ "$rec_n" =~ ^[0-9]+$ && "$rec_h" =~ ^[0-9a-f]{64}$ ]] || die "ledger: LENGTH is malformed"
  for f in "$l"/events/*.txt; do
    [ -e "$f" ] || break
    [ -f "$f" ] && [ ! -L "$f" ] || die "ledger: $f is not a regular file"
    n=$((n + 1))
    [ "${f##*/}" = "$(printf '%06d' "$n").txt" ] || die "ledger: event numbering has a gap or an unexpected file at $f"
    IFS= read -r line < "$f" || true
    case "$line" in "seq=$n prev=$prev "*) ;; *) die "ledger: the hash chain is broken at event $n (the ledger was modified)";; esac
    prev="$(r0p_sha "$f")"
    if [ "$n" -eq "$rec_n" ] && [ "$prev" != "$rec_h" ]; then die "ledger: event $n does not match the recorded length file (the ledger was modified)"; fi
  done
  [ "$n" -ge "$rec_n" ] || die "ledger: it holds $n events but $rec_n were recorded (the ledger was truncated)"
  R0P_LEDGER_N=$n; R0P_LEDGER_LAST="$prev"
}
r0p_ledger_lock() {
  local i
  for ((i = 0; i < 100; i++)); do
    if /bin/mkdir "$1/lock" 2>/dev/null; then return 0; fi
    /bin/sleep 0.1
  done
  die "the ledger is locked (a parallel run, or a stale lock: remove $1/lock only when no run is active)"
}
r0p_ledger_unlock() { /bin/rmdir "$1/lock" 2>/dev/null || true; }
# r0p_ledger_check LEDGER SCOPE_SHA ALLOW_REUSE : read-only early refusal (the atomic claim comes later)
r0p_ledger_check() {
  local l="$1" s="$2" allow="$3"
  r0p_ledger_prepare "$l"
  r0p_ledger_verify "$l"
  if [ -d "$l/claims/$s" ]; then
    if [ "$allow" = 1 ]; then echo "NOTE: --allow-reuse: this scope file was used before (see the ledger); reuse is recorded"
    else die "this scope file (sha256 $s) was already used (it is in the ledger). Write a NEW scope file for a new run, or pass --allow-reuse after deciding that a repeat is intended"; fi
  fi
}
# The locked sections run in a SUBSHELL so that a `die` inside (broken chain, symlink, existing event file) ends only the subshell: the parent then
# releases the lock and passes the exit status on. (An EXIT trap in the subshell is NOT used: bash does not run it reliably when the parent is itself
# inside its own EXIT trap, which is where the "ended" event is written.) A parent killed by a signal inside the section leaves lock/ behind; the
# next run then stops with a message that names it.
r0p_ledger_in_lock() { # LEDGER function args... : run the function under the lock in a subshell
  local rc=0
  r0p_ledger_lock "$1"
  ( "${@:2}" ) || rc=$?
  r0p_ledger_unlock "$1"
  [ "$rc" -eq 0 ] || exit "$rc"
}
# r0p_ledger_append LEDGER SCOPE_SHA MODE EVENT [DETAIL]
r0p_ledger_append_locked() {
  local l="$1" s="$2" seq prev f line
  if [ -L "$l" ] || [ -L "$l/events" ]; then die "ledger: the ledger path became a symlink; refusing"; fi
  r0p_ledger_verify "$l"
  seq=$((R0P_LEDGER_N + 1)); prev="$R0P_LEDGER_LAST"
  f="$l/events/$(printf '%06d' "$seq").txt"
  line="seq=$seq prev=$prev scope=$s time=$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ) mode=$3 event=$4${5:+ $5}"
  # O_EXCL (noclobber): an existing file or a symlink at the event path is never followed or overwritten
  ( set -C; printf '%s\n' "$line" > "$f" ) 2>/dev/null || die "ledger: $f already exists or is a symlink; refusing (nothing was followed or overwritten)"
  chmod 600 "$f"
  /bin/rm -f "$l/LENGTH.new"
  ( set -C; printf '%s %s\n' "$seq" "$(r0p_sha "$f")" > "$l/LENGTH.new" ) 2>/dev/null || die "ledger: cannot write LENGTH.new"
  /bin/mv -f "$l/LENGTH.new" "$l/LENGTH"
}
r0p_ledger_append() { r0p_ledger_in_lock "$1" r0p_ledger_append_locked "$@"; }
# r0p_ledger_claim LEDGER SCOPE_SHA ALLOW_REUSE MODE DETAIL : ATOMICALLY claim the scope (mkdir), then record "started". Exactly one of several
# parallel runs of the same scope wins; the others stop here, before any use of the owner's session. A claim is never released.
r0p_ledger_claim_locked() {
  local l="$1" s="$2" allow="$3"
  if /bin/mkdir "$l/claims/$s" 2>/dev/null; then :
  elif [ "$allow" = 1 ]; then echo "NOTE: --allow-reuse: the scope was claimed before; this repeat is recorded"
  else die "this scope file (sha256 $s) is already claimed (used before, or by a parallel run); refusing"; fi
}
r0p_ledger_claim() {
  r0p_ledger_prepare "$1"
  r0p_ledger_in_lock "$1" r0p_ledger_claim_locked "$1" "$2" "$3"
  r0p_ledger_append "$1" "$2" "$4" started "${5:-}${3:+ allow_reuse=$3}"
}

# distribution gate -----------------------------------------------------------------------------------------------------
# r0p_gate PLATFORM DISTRIBUTION(0/1) EXPECTED_GATE_SHA EXPECTED_GATE_TOOL_SHA : the repository copies of the gate file and of the gate
# tool must be the ones the scope binds; the gate is run on the repository copy (never another --gate); its list of open decisions is
# printed; with DISTRIBUTION=1 an open blocker (or a malformed gate file) refuses the run; otherwise a loud statement is printed.
r0p_gate() {
  local platform="$1" dist="$2" gsha="$3" tsha="$4" gj="$SELF_DIR/../distribution-gate.json" gp="$SELF_DIR/../check-distribution-gate.py" rc=0
  [ -f "$gj" ] && [ -f "$gp" ] || die "the distribution gate files are missing next to packaging/"
  [ "$(r0p_sha "$gj")" = "$gsha" ] || die "scope file: gate_sha256 does not match packaging/distribution-gate.json ($(r0p_sha "$gj"))"
  [ "$(r0p_sha "$gp")" = "$tsha" ] || die "scope file: gate_tool_sha256 does not match packaging/check-distribution-gate.py ($(r0p_sha "$gp"))"
  [ -x /usr/bin/python3 ] || die "/usr/bin/python3 not found: the distribution gate cannot be run"
  if [ "$dist" = 1 ]; then
    /usr/bin/python3 -I "$gp" --platform "$platform" --distribution || rc=$?
    [ "$rc" -eq 0 ] || die "the distribution gate REFUSES a distribution signing for platform $platform (exit $rc): decisions are OPEN or the gate file is malformed"
    echo "distribution gate: passed for platform $platform (written decisions exist; this is not a legal conclusion)"
  else
    /usr/bin/python3 -I "$gp" --platform "$platform" || rc=$?
    [ "$rc" -eq 0 ] || die "the distribution gate file is malformed (exit $rc)"
    echo "*** PRIVATE-CANDIDATE SIGNING: signing and notarization do NOT imply permission to distribute. The decisions above are not cleared by this run."
  fi
}
