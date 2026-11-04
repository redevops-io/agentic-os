# signing-common.sh  (version r0prime-common-2)  -- SOURCED by the R0-prime signing scripts; it is not run on its own.
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

R0P_BASE_ALLOW=" PATH HOME LANG LC_ALL TERM PWD OLDPWD SHLVL _ R0P_REEXEC "
R0P_FIXED_PATH="/usr/bin:/bin:/usr/sbin:/sbin"
R0P_TMP=""

r0p_sha() { /usr/bin/shasum -a 256 "$1" | /usr/bin/awk '{print $1}'; }
r0p_sha_str() { printf '%s' "$1" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}'; }

# r0p_scrub "<space separated extra variable names that may pass>" "$@"
# The environment of the running script must contain ONLY the base allowlist plus the named extras and no exported function. If it
# does not, the script re-executes itself ONCE under `env -i` with exactly those variables. There is no flag that skips this check:
# R0P_REEXEC only prevents a loop, and if it is set while the environment is still not clean the script stops.
r0p_scrub() {
  local extra=" $1 " v bad="" allow keep=() i
  shift
  allow="$R0P_BASE_ALLOW${extra# }"
  for v in $(compgen -A export); do
    case "$allow" in *" $v "*) ;; *) bad="$bad $v";; esac
  done
  if [ -n "$(declare -xf 2>/dev/null)" ]; then bad="$bad <exported-function>"; fi
  if [ -z "$bad" ]; then return 0; fi
  if [ "${R0P_REEXEC:-}" = 1 ]; then echo "ERROR: the environment is still not the allowlist after the scrub (extra:$bad); refusing" >&2; exit 1; fi
  for v in $extra; do
    if [ -n "${!v+x}" ]; then keep+=("$v=${!v}"); fi
  done
  exec env -i "PATH=${PATH:-/usr/bin:/bin}" "HOME=${HOME:-/nonexistent}" LANG=C LC_ALL=C "TERM=${TERM:-dumb}" \
       ${keep[@]+"${keep[@]}"} R0P_REEXEC=1 "${BASH:-/bin/bash}" "$SELF" "$@"
}

# r0p_fix_path [extra-dirs]  : PATH becomes the fixed system path (+ the validated extra directories). R0P_ALLOW_NONSYSTEM_TOOLS=stub-test
# keeps the inherited PATH (unit tests only; the scripts print it in their evidence).
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

# one-time ledger -------------------------------------------------------------------------------------------------------
# r0p_ledger_check LEDGER SCOPE_SHA ALLOW_REUSE(0/1): a scope that appears in the ledger is refused unless reuse was asked for.
r0p_ledger_check() {
  local l="$1" s="$2" allow="$3"
  [ -n "$l" ] || die "--real needs --ledger FILE (an append-only record of used scope files, kept outside the repository)"
  case "$l" in /*) ;; *) die "--ledger must be an absolute path";; esac
  [ -d "$(dirname "$l")" ] || die "the directory of --ledger does not exist"
  if [ -e "$l" ] || [ -L "$l" ]; then
    [ -f "$l" ] && [ ! -L "$l" ] || die "--ledger must be a regular file (not a symlink)"
    [ "$(/usr/bin/stat -f %u "$l")" = "$(/usr/bin/id -u)" ] || die "--ledger is owned by someone else"
    if /usr/bin/grep -q "^scope=$s " "$l"; then
      if [ "$allow" = 1 ]; then echo "NOTE: --allow-reuse: this scope file was used before (see the ledger); reuse is recorded"
      else die "this scope file (sha256 $s) was already used (it is in the ledger). Write a NEW scope file for a new run, or pass --allow-reuse after deciding that a repeat is intended"; fi
    fi
  fi
}
# r0p_ledger_append LEDGER SCOPE_SHA MODE EVENT [DETAIL]  (the file is created 0600 by umask 077)
r0p_ledger_append() {
  printf 'scope=%s time=%s mode=%s event=%s%s\n' "$2" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$3" "$4" "${5:+ $5}" >> "$1"
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
