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
R0P_BASE_ALLOW=" PATH HOME LANG LC_ALL TERM PWD OLDPWD SHLVL _ "
R0P_FIXED_PATH="/usr/bin:/bin:/usr/sbin:/sbin"
R0P_TMP=""
r0p_scrub() {
  local extra=" $1 " v bad="" allow i maxfd
  allow="$R0P_BASE_ALLOW${extra# }"
  [[ "$-" == *p* && "${R0P_STARTED_OK:-}" == 1 ]] || { echo "ERROR: the script is not the privileged-mode copy started by its own first block; refusing" >&2; exit 1; }
  for v in $(builtin compgen -A export); do
    case "$allow" in *" $v "*) ;; *) bad="$bad $v";; esac
  done
  if /usr/bin/env | /usr/bin/grep -q '^BASH_FUNC_'; then bad="$bad <exported-function>"; fi
  if [ -n "$bad" ]; then echo "ERROR: the environment is not the allowlist (extra:$bad); refusing" >&2; exit 1; fi
  # file descriptors above 9 (3 to 9 were closed by the re-exec) up to the soft limit, at most 1024; 255 is bash's own script descriptor and is left alone
  maxfd="$(builtin ulimit -n)"; [[ "$maxfd" =~ ^[0-9]+$ ]] || maxfd=256; [ "$maxfd" -le 1024 ] || maxfd=1024
  for ((i = 10; i < maxfd; i++)); do [ "$i" -eq 255 ] || eval "exec $i>&-"; done
}

# r0p_fix_path [extra-dirs]  : PATH becomes the fixed system path (+ the validated extra directories). R0P_ALLOW_NONSYSTEM_TOOLS=stub-test
# keeps the inherited PATH (unit tests only) and is REFUSED in --real (r0p_no_test_switch).
R0P_STUBS=0
r0p_fix_path() {
  local extra="${1:-}" d
  if [ "${R0P_ALLOW_NONSYSTEM_TOOLS:-}" = "stub-test" ]; then R0P_STUBS=1; PATH="$R0P_ORIG_PATH"; export PATH; return 0; fi
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
#   claims/<artifact key> (RC3) one directory per INPUT ARTIFACT (dmg-<sha256 of the input dmg>, win-<stage>-<hash of the sorted inputs>,
#                      manifest-<sha256 of the manifest>), claimed the same way: a second real run for the same artifact is refused even when the
#                      scope FILE is new (a scope rewritten one second later is a different file), unless --allow-reuse is given; the reuse is
#                      recorded in the event (artifact_reuse=1). A failed run burns the artifact claim exactly as it burns the scope claim.
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
R0P_LEDGER_N=0; R0P_LEDGER_LAST="$R0P_ZERO64"; R0P_LEDGER_STARTED=" "; R0P_LEDGER_ARTIFACTS=" "
r0p_ledger_verify() {
  local l="$1" n=0 prev="$R0P_ZERO64" f rec_n rec_h line sc ak
  R0P_LEDGER_STARTED=" "; R0P_LEDGER_ARTIFACTS=" "
  read -r rec_n rec_h < "$l/LENGTH" 2>/dev/null || die "ledger: LENGTH is missing or unreadable"
  [[ "$rec_n" =~ ^[0-9]+$ && "$rec_h" =~ ^[0-9a-f]{64}$ ]] || die "ledger: LENGTH is malformed"
  for f in "$l"/events/*.txt; do
    [ -e "$f" ] || break
    [ -f "$f" ] && [ ! -L "$f" ] || die "ledger: $f is not a regular file"
    n=$((n + 1))
    [ "${f##*/}" = "$(printf '%06d' "$n").txt" ] || die "ledger: event numbering has a gap or an unexpected file at $f"
    IFS= read -r line < "$f" || true
    case "$line" in "seq=$n prev=$prev "*) ;; *) die "ledger: the hash chain is broken at event $n (the ledger was modified)";; esac
    case "$line" in *" event=started"*)
      sc="${line#* scope=}"; R0P_LEDGER_STARTED="$R0P_LEDGER_STARTED${sc%% *} "
      case "$line" in *" artifact="*) ak="${line#* artifact=}"; R0P_LEDGER_ARTIFACTS="$R0P_LEDGER_ARTIFACTS${ak%% *} ";; esac;;
    esac
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
# "used" is decided from BOTH the claim directory and the verified chain (round 6, R6-02): a scope that the chain records as started but whose claim
# directory is missing means someone removed the claim (tamper) and is refused even with --allow-reuse.
r0p_ledger_tamper_check() { # LEDGER SCOPE [ARTIFACT_KEY] : after r0p_ledger_verify
  case "$R0P_LEDGER_STARTED" in
    *" $2 "*) [ -d "$1/claims/$2" ] || die "ledger: the chain records this scope as started but its claim is missing (the claims directory was tampered with); refusing";;
  esac
  if [ -n "${3:-}" ]; then
    case "$R0P_LEDGER_ARTIFACTS" in
      *" $3 "*) [ -d "$1/claims/$3" ] || die "ledger: the chain records this artifact as started but its claim is missing (the claims directory was tampered with); refusing";;
    esac
  fi
}
# an artifact key names the input of the run: dmg-<64 hex>, win-<stage>-<64 hex>, manifest-<64 hex> (never anything else: it is a directory name)
r0p_ledger_artifact_ok() { [[ "$1" =~ ^(dmg|manifest)-[0-9a-f]{64}$ || "$1" =~ ^win-(inner|container)-[0-9a-f]{64}$ ]] || die "ledger: invalid artifact key"; }
# r0p_ledger_check LEDGER SCOPE_SHA ALLOW_REUSE : read-only early refusal (the atomic claim comes later)
r0p_ledger_check() {
  local l="$1" s="$2" allow="$3" art="${4:-}"
  [ -z "$art" ] || r0p_ledger_artifact_ok "$art"
  r0p_ledger_prepare "$l"
  r0p_ledger_verify "$l"
  r0p_ledger_tamper_check "$l" "$s" "$art"
  if [ -d "$l/claims/$s" ]; then
    if [ "$allow" = 1 ]; then echo "NOTE: --allow-reuse: this scope file was used before (see the ledger); reuse is recorded"
    else die "this scope file (sha256 $s) was already used (it is in the ledger). Write a NEW scope file for a new run, or pass --allow-reuse after deciding that a repeat is intended"; fi
  fi
  if [ -n "$art" ] && [ -d "$l/claims/$art" ]; then
    if [ "$allow" = 1 ]; then echo "NOTE: --allow-reuse: this input artifact ($art) was used by an earlier real run (see the ledger); the repeat, including any second upload or signing request, is recorded"
    else die "this input artifact ($art) was already used by an earlier real run (it is in the ledger), even though the scope file is new. A second signing or notarization of the same artifact needs --allow-reuse, decided and recorded by the owner"; fi
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
  local l="$1" s="$2" allow="$3" art="${4:-}"
  r0p_ledger_verify "$l"
  r0p_ledger_tamper_check "$l" "$s" "$art"
  # the ARTIFACT claim comes first, so a refused repeat of an artifact burns nothing new
  if [ -n "$art" ]; then
    if /bin/mkdir "$l/claims/$art" 2>/dev/null; then :
    elif [ "$allow" = 1 ]; then echo "NOTE: --allow-reuse: the input artifact $art was claimed before; this repeat is recorded"
    else die "this input artifact ($art) is already claimed (used by an earlier real run, or by a parallel run); refusing"; fi
  fi
  if /bin/mkdir "$l/claims/$s" 2>/dev/null; then :
  elif [ "$allow" = 1 ]; then echo "NOTE: --allow-reuse: the scope was claimed before; this repeat is recorded"
  else die "this scope file (sha256 $s) is already claimed (used before, or by a parallel run); refusing"; fi
}
# r0p_ledger_claim LEDGER SCOPE_SHA ALLOW_REUSE MODE DETAIL [ARTIFACT_KEY]
r0p_ledger_claim() {
  local art="${6:-}" reuse=""
  [ -z "$art" ] || r0p_ledger_artifact_ok "$art"
  r0p_ledger_prepare "$1"
  [ -z "$art" ] || { [ ! -d "$1/claims/$art" ] || reuse=" artifact_reuse=1"; }
  r0p_ledger_in_lock "$1" r0p_ledger_claim_locked "$1" "$2" "$3" "$art"
  r0p_ledger_append "$1" "$2" "$4" started "${5:-}${art:+ artifact=$art}${reuse}${3:+ allow_reuse=$3}"
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
    /usr/bin/python3 -I "$gp" --platform "$platform" --distribution --about-slots || rc=$?
    [ "$rc" -eq 0 ] || die "the distribution gate REFUSES a distribution signing for platform $platform (exit $rc): decisions are OPEN, a legal slot has no approved text, a record has no valid detached signature, or the gate file is malformed"
    echo "distribution gate: passed for platform $platform (written decisions with valid detached signatures exist; this is not a legal conclusion)"
  else
    /usr/bin/python3 -I "$gp" --platform "$platform" --about-slots || rc=$?
    [ "$rc" -eq 0 ] || die "the distribution gate file is malformed or an About slot text does not match (exit $rc)"
    echo "*** PRIVATE-CANDIDATE SIGNING: signing and notarization do NOT imply permission to distribute. The decisions above are not cleared by this run."
  fi
}

# About record of the artifact being signed (RC3) --------------------------------------------------------------------------
# The code and data this check uses are pinned by the authorization scope (scope_version 3), like every other file that decides or
# executes during a run: check_about.py, generate_about.py, agentic_os/about_record.py, packaging/legal-allowed-signers and (already)
# packaging/check-distribution-gate.py. A hash in the record or in the gate file proves consistency, not authorship.
#
# r0p_about_pins CHECK_SHA GEN_SHA REC_SHA SIGNERS_SHA : the repository copies must be the ones the scope binds (dies otherwise).
r0p_about_pins() {
  local pk="$SELF_DIR/.." root="$SELF_DIR/../.." h
  h="$(r0p_sha "$pk/notices/check_about.py")";        [ "$h" = "$1" ] || die "scope file: check_about_sha256 does not match packaging/notices/check_about.py ($h)"
  h="$(r0p_sha "$pk/notices/generate_about.py")";     [ "$h" = "$2" ] || die "scope file: generate_about_sha256 does not match packaging/notices/generate_about.py ($h)"
  h="$(r0p_sha "$root/agentic_os/about_record.py")";  [ "$h" = "$3" ] || die "scope file: about_record_sha256 does not match agentic_os/about_record.py ($h)"
  h="$(r0p_sha "$pk/legal-allowed-signers")";         [ "$h" = "$4" ] || die "scope file: legal_signers_sha256 does not match packaging/legal-allowed-signers ($h)"
}
# r0p_about_check LICENSES_DIR DISTRIBUTION(0/1) BUILD_COMMIT CHECK_SHA GEN_SHA REC_SHA GATE_TOOL_SHA SIGNERS_SHA : the Licenses folder INSIDE the
# artifact (the app's Contents/Resources/Licenses taken from the dmg copy; the Licenses folder of the installer tree on Windows) must hold an
# about.json that passes check_about.py against the repository copies of the gate file, identity, versions and the folder's own manifest, and
# whose build.source.commit is the authorized build_commit. The checker runs as `python -I -B` from a PRIVATE COPY of the pinned files, made
# in the private work directory and re-hashed after copying: it imports nothing unpinned. With DISTRIBUTION=1 the record must be a distribution
# record without a pending or draft slot and every legal record must carry a valid detached signature (signer list pinned by fingerprint).
# Returns non-zero (the caller fails) instead of dying, so the evidence of the run records the reason.
r0p_about_check() {
  local dir="$1" dist="$2" commit="$3" csha="$4" gsha="$5" rsha="$6" tsha="$7" ssha="$8"
  local pk="$SELF_DIR/.." root="$SELF_DIR/../.." t rc=0 h
  [ -f "$pk/notices/check_about.py" ] && [ -f "$root/agentic_os/about_record.py" ] || { echo "ERROR: packaging/notices/check_about.py or agentic_os/about_record.py is missing next to packaging/" >&2; return 1; }
  [ -d "$dir" ] && [ ! -L "$dir" ] || { echo "ERROR: the artifact has no Licenses folder to check ($dir)" >&2; return 1; }
  [ -f "$dir/about.json" ] && [ ! -L "$dir/about.json" ] || { echo "ERROR: the artifact has no Licenses/about.json (an RC3 artifact must carry the About record)" >&2; return 1; }
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo "ERROR: the authorized build commit is not a 40-hex id" >&2; return 1; }
  [ -x /usr/bin/python3 ] || { echo "ERROR: /usr/bin/python3 not found: the About record cannot be checked" >&2; return 1; }
  [ -n "${R0P_TMP:-}" ] || r0p_mktmp
  t="$R0P_TMP/about-check"
  mkdir -p "$t/agentic_os" "$t/packaging/notices" || { echo "ERROR: cannot create the private work directory for the About check" >&2; return 1; }
  cat "$pk/notices/check_about.py"       > "$t/packaging/notices/check_about.py"       || return 1
  cat "$pk/notices/generate_about.py"    > "$t/packaging/notices/generate_about.py"    || return 1
  cat "$root/agentic_os/about_record.py" > "$t/agentic_os/about_record.py"             || return 1
  cat "$pk/check-distribution-gate.py"   > "$t/packaging/check-distribution-gate.py"   || return 1
  cat "$pk/legal-allowed-signers"        > "$t/packaging/legal-allowed-signers"        || return 1
  h="$(r0p_sha "$t/packaging/notices/check_about.py")";     [ "$h" = "$csha" ] || { echo "ERROR: the private copy of check_about.py ($h) is not the one the scope binds" >&2; return 1; }
  h="$(r0p_sha "$t/packaging/notices/generate_about.py")";  [ "$h" = "$gsha" ] || { echo "ERROR: the private copy of generate_about.py ($h) is not the one the scope binds" >&2; return 1; }
  h="$(r0p_sha "$t/agentic_os/about_record.py")";           [ "$h" = "$rsha" ] || { echo "ERROR: the private copy of about_record.py ($h) is not the one the scope binds" >&2; return 1; }
  h="$(r0p_sha "$t/packaging/check-distribution-gate.py")"; [ "$h" = "$tsha" ] || { echo "ERROR: the private copy of the gate tool ($h) is not the one the scope binds" >&2; return 1; }
  h="$(r0p_sha "$t/packaging/legal-allowed-signers")";      [ "$h" = "$ssha" ] || { echo "ERROR: the private copy of legal-allowed-signers ($h) is not the one the scope binds" >&2; return 1; }
  if [ "$dist" = 1 ]; then
    /usr/bin/python3 -I -B "$t/packaging/notices/check_about.py" --licenses-dir "$dir" --root "$root" --code-root "$t" \
      --allowed-signers "$t/packaging/legal-allowed-signers" --expect-commit "$commit" --distribution || rc=$?
  else
    /usr/bin/python3 -I -B "$t/packaging/notices/check_about.py" --licenses-dir "$dir" --root "$root" --code-root "$t" \
      --allowed-signers "$t/packaging/legal-allowed-signers" --expect-commit "$commit" || rc=$?
  fi
  [ "$rc" -eq 0 ] || { echo "ERROR: the About record inside the artifact failed check_about.py (exit $rc)" >&2; return 1; }
  return 0
}
