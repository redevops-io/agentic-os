#!/usr/bin/env bash
# sign_and_notarize_r0prime_macos_v3.sh  (script version r0prime-3)
#
# Sign, (optionally) notarize + staple, and verify the macOS arm64 .dmg of "Agentic Apps R0-prime" WITHOUT modifying the input.
# Derived from the frozen sign_and_notarize_macos_v2.sh (v0.3.2 line; independently reviewed, APPROVE). The frozen script is
# read-only and unchanged. Every difference is listed in packaging/signing/DIFF-vs-v2.md. Procedure, gates, authorization
# template and risks: packaging/signing/SIGNING-PROCEDURE-R0PRIME.md.
#
# THIS SCRIPT HAS NEVER BEEN RUN WITH A REAL IDENTITY OR AGAINST APPLE. It was tested with stub tools and with the ad-hoc
# identity '-' on a copy. Signing with a real identity and notarization need the owner's separate, explicit authorization.
#
# MODES (exactly one; the default is --dry-run)
#   --dry-run      DEFAULT. Validates the arguments, the paths and the sha256 of the input dmg (a read of the file, nothing else),
#                  derives every name from packaging/identity.json and PRINTS each command that would run. Creates nothing,
#                  mounts nothing, signs nothing, contacts nobody, never reads a keychain.
#   --adhoc-test   Runs the WHOLE sequence with the ad-hoc identity '-' (no timestamp, no keychain, no Apple) on a COPY of the
#                  input inside --out-dir, skips notarization and stapling, and writes the result as ADHOC-TEST-ONLY_<name>.dmg.
#                  Reviewers can run it without any secret. spctl is EXPECTED to reject its output.
#   --real         Real signing. Needs ALL of:
#                    --i-have-owner-authorization <sha256 of the scope file>  and  --scope-file FILE (see below)
#                    --expected-sha256 <sha256 of the input dmg>              (also listed in the scope file)
#                    environment R0P_CODESIGN_IDENTITY = the Developer ID Application identity (supplied by the owner)
#                    for --notarize: R0P_NOTARY_PROFILE = the notarytool keychain profile NAME (the script never sees credentials)
#                  The identity and profile names are read from the environment only (never from the command line, never from a
#                  file) and are never written by this script to the transcript or the evidence (only sha256(identity) is recorded); note that the certificate's
#                  own name appears in `codesign -dvvv` output, which is kept as evidence, as in the frozen v0.3.2 evidence.
#
# SCOPE FILE (what the owner authorizes; plain KEY=VALUE lines, '#' comments, unknown or duplicate keys are refused;
# its sha256 is the argument of --i-have-owner-authorization; template: packaging/signing/AUTHORIZATION-TEMPLATE.md)
#   scope_version=1  valid_from=YYYY-MM-DD  valid_until=YYYY-MM-DD (UTC, inclusive)  script_sha256=<this file>
#   entitlements_sha256=<plist>  input_dmg_sha256=<dmg>  identity_sha256=<sha256 of the exact identity string>
#   team_id=<10 chars>  notarize=yes|no  upload_to=apple-notary|none
# Real mode refuses if the date is outside the window, if any value differs from what this run would use, if notarize=yes is
# not matched by --notarize (and vice versa), or if --notarize is requested without upload_to=apple-notary.
#
# SAFETY (kept from v2 unless noted in DIFF-vs-v2.md)
#   * Real identity only in --real; any R0P_CODESIGN_IDENTITY in the environment of a non-real mode is REFUSED (no accidental use).
#   * The input dmg and anything inside a candidate directory are never written: the input must be a COPY (refused if its folder
#     looks like a candidate/evidence set or is named by --protected-dir). Everything this script writes lives under --out-dir
#     (must not exist or must be empty). It never uses rm -rf, sudo or `security`, never `set -x`, and never prints secrets.
#   * The script re-executes itself under `env -i` with an allowlist (PATH, HOME, LANG, TERM, R0P_*): cloud and token variables of
#     the caller never reach codesign, notarytool or hdiutil.
#   * In --real the tools must resolve to system locations (R0P_ALLOW_NONSYSTEM_TOOLS=stub-test is for the unit tests only and is
#     printed in the evidence).
#   * Inside-out signing, never --deep: sidecar (entitlements, hardened runtime, secure timestamp), launcher, bundle, then the dmg.
#     Verification is per binary and for the bundle (--verify --strict), without --deep.
#   * Notarization uploads the signed dmg to Apple. One submission, time-boxed (--notary-timeout), no automatic retry; a failed or
#     timed-out submission is never reused and never re-uploaded without the owner's explicit OK (SIGNING-PROCEDURE-R0PRIME.md).
#   * A transcript of everything printed is kept in --out-dir/TRANSCRIPT.txt; its sha256 is written to TRANSCRIPT.sha256 and printed.
#
# bash 3.2 compatible (macOS default).
#
# Usage:
#   sign_and_notarize_r0prime_macos_v3.sh --input-dmg PATH --expected-sha256 HEX --out-dir DIR
#       [--dry-run | --adhoc-test | --real --i-have-owner-authorization SHA --scope-file FILE [--notarize]]
#       [--entitlements FILE] [--protected-dir DIR]... [--notary-timeout 30m]
case "$-" in *x*) echo "ERROR: xtrace is on; refusing (it could print secrets)" >&2; exit 1;; esac
set +x
set -euo pipefail
umask 077

SCRIPT_VERSION="r0prime-3"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
SELF_DIR="$(dirname "$SELF")"

# ---------------------------------------------------------------- scrubbed re-exec (allowlist environment)
if [ "${R0P_SCRUBBED:-}" != 1 ]; then
  KEEP=()
  for v in $(compgen -A export | grep '^R0P_' || true); do KEEP+=("$v=${!v}"); done
  exec env -i "PATH=${PATH:-/usr/bin:/bin}" "HOME=${HOME:-/nonexistent}" LANG=C LC_ALL=C "TERM=${TERM:-dumb}" \
       ${KEEP[@]+"${KEEP[@]}"} R0P_SCRUBBED=1 "${BASH:-/bin/bash}" "$SELF" "$@"
fi
export LC_ALL=C

die() { echo "ERROR: $*" >&2; exit 1; }
usage() { sed -n '/^# Usage:/,/^case /p' "$SELF" | sed '$d' | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }

INPUT_DMG=""; EXPECTED_SHA=""; OUT_DIR=""; ENTITLEMENTS="$SELF_DIR/../macos/sidecar.entitlements.plist"
MODE_DRY=0; MODE_ADHOC=0; MODE_REAL=0; AUTH_SHA=""; SCOPE_FILE=""; DO_NOTARIZE=0
NOTARY_TIMEOUT="30m"; PROTECTED=()

while [ $# -gt 0 ]; do
  case "$1" in
    --input-dmg) [ $# -ge 2 ] || die "--input-dmg needs a value"; INPUT_DMG="$2"; shift 2;;
    --expected-sha256) [ $# -ge 2 ] || die "--expected-sha256 needs a value"; EXPECTED_SHA="$2"; shift 2;;
    --out-dir) [ $# -ge 2 ] || die "--out-dir needs a value"; OUT_DIR="$2"; shift 2;;
    --entitlements) [ $# -ge 2 ] || die "--entitlements needs a value"; ENTITLEMENTS="$2"; shift 2;;
    --protected-dir) [ $# -ge 2 ] || die "--protected-dir needs a value"; PROTECTED+=("$2"); shift 2;;
    --notary-timeout) [ $# -ge 2 ] || die "--notary-timeout needs a value"; NOTARY_TIMEOUT="$2"; shift 2;;
    --i-have-owner-authorization) [ $# -ge 2 ] || die "--i-have-owner-authorization needs the sha256 of the scope file"; AUTH_SHA="$2"; shift 2;;
    --scope-file) [ $# -ge 2 ] || die "--scope-file needs a value"; SCOPE_FILE="$2"; shift 2;;
    --notarize) DO_NOTARIZE=1; shift;;
    --dry-run) MODE_DRY=1; shift;;
    --adhoc-test) MODE_ADHOC=1; shift;;
    --real) MODE_REAL=1; shift;;
    -h|--help) usage 0;;
    *) echo "unknown argument: $1" >&2; usage 2;;
  esac
done

# ---------------------------------------------------------------- mode policy
[ $((MODE_DRY + MODE_ADHOC + MODE_REAL)) -le 1 ] || die "give at most one of --dry-run, --adhoc-test, --real"
if [ $((MODE_DRY + MODE_ADHOC + MODE_REAL)) -eq 0 ]; then MODE_DRY=1; fi
if [ "$MODE_REAL" = 1 ]; then MODE="real"; elif [ "$MODE_ADHOC" = 1 ]; then MODE="adhoc-test"; else MODE="dry-run"; fi
PLAN=0; [ "$MODE" = dry-run ] && PLAN=1
[ "$(uname -s)" = Darwin ] || die "macOS only"
[ "$(id -u)" -ne 0 ] || die "do not run as root"

IDENTITY_ENV="${R0P_CODESIGN_IDENTITY:-}"
NOTARY_PROFILE="${R0P_NOTARY_PROFILE:-}"
if [ "$MODE" != real ]; then
  [ -z "$IDENTITY_ENV" ] || die "R0P_CODESIGN_IDENTITY is set but the mode is '$MODE': a real identity is used only with --real (unset the variable; this refusal prevents accidental signing)"
  [ -z "$NOTARY_PROFILE" ] || die "R0P_NOTARY_PROFILE is set but the mode is '$MODE': unset it (notarization exists only with --real --notarize)"
  [ "$DO_NOTARIZE" = 0 ] || die "--notarize exists only with --real"
  [ -z "$AUTH_SHA" ] && [ -z "$SCOPE_FILE" ] || die "--i-have-owner-authorization and --scope-file are used only with --real"
fi

# ---------------------------------------------------------------- identity of the line (from packaging/identity.json, same rules as packaging/tools/identity.py)
IDJSON="$SELF_DIR/../identity.json"
[ -f "$IDJSON" ] || die "packaging/identity.json not found next to the script ($IDJSON)"
idj() { plutil -extract "$1" raw -o - "$IDJSON" 2>/dev/null; }
PRODUCT="$(idj r0prime.product_name)"; VERSION="$(idj r0prime.version)"; BUNDLE_VERSION="$(idj r0prime.bundle_version)"
BUNDLE_ID="$(idj r0prime.bundle_identifier)"; SIDECAR_ID="$(idj r0prime.sidecar_signing_identifier)"
MAIN_EXE="$(idj r0prime.main_binary_name)"; SIDECAR_EXE="$(idj r0prime.sidecar_binary_name)"
FROZEN_PRODUCT="$(idj frozen.product_name)"; FROZEN_BUNDLE_ID="$(idj frozen.bundle_identifier)"; FROZEN_MAIN="$(idj frozen.main_binary_name)"
[[ "$PRODUCT" =~ ^[A-Za-z0-9][A-Za-z0-9\ ._-]{0,63}$ ]] || die "identity.json: product_name is empty or has unexpected characters"
[[ "$VERSION" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] && [[ "$BUNDLE_VERSION" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] || die "identity.json: version/bundle_version are not 1 to 3 integers"
[[ "$BUNDLE_ID" =~ ^[A-Za-z0-9.-]{3,100}$ ]] && [[ "$SIDECAR_ID" =~ ^[A-Za-z0-9.-]{3,110}$ ]] || die "identity.json: bundle/sidecar identifiers have unexpected characters"
[[ "$MAIN_EXE" =~ ^[A-Za-z0-9._-]{1,40}$ ]] && [[ "$SIDECAR_EXE" =~ ^[A-Za-z0-9._-]{1,40}$ ]] || die "identity.json: binary names have unexpected characters"
[ "$PRODUCT" != "$FROZEN_PRODUCT" ] && [ "$BUNDLE_ID" != "$FROZEN_BUNDLE_ID" ] && [ "$MAIN_EXE" != "$FROZEN_MAIN" ] \
  || die "identity.json does not describe a line distinct from the frozen v0.3.2 line; refusing (decision 8: never reuse frozen identities)"
APP_NAME="$PRODUCT.app"
DMG_NAME="${PRODUCT}_${VERSION}_aarch64.dmg"          # derived exactly as packaging/tools/identity.py does
VOLNAME="$PRODUCT"
RELEASE_LINE="$(idj release_line)"
EXPECTED_ENT_JSON='{"com.apple.security.cs.disable-library-validation":true}'

# ---------------------------------------------------------------- argument policy
[ -n "$INPUT_DMG" ] && [ -f "$INPUT_DMG" ] && [ ! -L "$INPUT_DMG" ] || die "--input-dmg must be an existing regular file (not a symlink)"
[[ "$EXPECTED_SHA" =~ ^[0-9a-fA-F]{64}$ ]] || die "--expected-sha256 must be 64 hex chars"
EXPECTED_SHA="$(echo "$EXPECTED_SHA" | tr 'A-F' 'a-f')"
[ -n "$OUT_DIR" ] || die "--out-dir is required"
[ -f "$ENTITLEMENTS" ] || die "entitlements file not found: $ENTITLEMENTS"
plutil -lint "$ENTITLEMENTS" >/dev/null || die "entitlements file is not a valid plist"
[[ "$NOTARY_TIMEOUT" =~ ^[0-9]{1,3}[smh]$ ]] || die "--notary-timeout must look like 90s, 30m or 2h"

sha() { shasum -a 256 "$1" | awk '{print $1}'; }
sha_str() { printf '%s' "$1" | shasum -a 256 | awk '{print $1}'; }
abspath() { ( cd "$(dirname "$1")" 2>/dev/null && printf '%s/%s' "$(pwd -P)" "$(basename "$1")" ); }
ENT_SHA="$(sha "$ENTITLEMENTS")"
SELF_SHA="$(sha "$SELF")"

# ---------------------------------------------------------------- real mode: identity, profile, scope file
IDENTITY="-"; TS_FLAG="--timestamp=none"
if [ "$PLAN" = 1 ]; then TS_FLAG="--timestamp"; fi   # the plan shows the --real sequence
if [ "$MODE" = real ]; then
  [[ "$AUTH_SHA" =~ ^[0-9a-fA-F]{64}$ ]] || die "--real needs --i-have-owner-authorization <sha256 of the scope file>"
  AUTH_SHA="$(echo "$AUTH_SHA" | tr 'A-F' 'a-f')"
  [ -n "$SCOPE_FILE" ] && [ -f "$SCOPE_FILE" ] && [ ! -L "$SCOPE_FILE" ] || die "--real needs --scope-file FILE (the file the owner's authorization names)"
  [ "$(sha "$SCOPE_FILE")" = "$AUTH_SHA" ] || die "the scope file's sha256 does not equal the value given to --i-have-owner-authorization"
  [ -n "$IDENTITY_ENV" ] || die "--real needs R0P_CODESIGN_IDENTITY in the environment (the identity is never a command-line argument)"
  case "$IDENTITY_ENV" in "Developer ID Application:"*) ;; *) die "R0P_CODESIGN_IDENTITY must start with 'Developer ID Application:'";; esac
  case "$IDENTITY_ENV" in *[[:cntrl:]]*) die "R0P_CODESIGN_IDENTITY contains control characters";; esac
  if [ "$DO_NOTARIZE" = 1 ]; then
    [ -n "$NOTARY_PROFILE" ] || die "--notarize needs R0P_NOTARY_PROFILE (a notarytool keychain profile NAME)"
    [[ "$NOTARY_PROFILE" =~ ^[A-Za-z0-9._\ -]{1,64}$ ]] || die "R0P_NOTARY_PROFILE has unexpected characters"
  else
    [ -z "$NOTARY_PROFILE" ] || die "R0P_NOTARY_PROFILE is set but --notarize was not given; refusing the ambiguity"
  fi
  IDENTITY="$IDENTITY_ENV"; TS_FLAG="--timestamp"
  # --- parse the scope file strictly (no sourcing)
  S_scope_version=""; S_valid_from=""; S_valid_until=""; S_script_sha256=""; S_entitlements_sha256=""; S_input_dmg_sha256=""
  S_identity_sha256=""; S_team_id=""; S_notarize=""; S_upload_to=""
  SEEN=" "
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue;; esac
    key="${line%%=*}"; val="${line#*=}"
    [ "$key" != "$line" ] || die "scope file: line without '=': refusing"
    case "$val" in *[[:cntrl:]]*) die "scope file: control character in a value";; esac
    case "$SEEN" in *" $key "*) die "scope file: duplicate key $key";; esac
    SEEN="$SEEN$key "
    case "$key" in
      scope_version|valid_from|valid_until|script_sha256|entitlements_sha256|input_dmg_sha256|identity_sha256|team_id|notarize|upload_to) eval "S_$key=\$val";;
      *) die "scope file: unknown key '$key'";;
    esac
  done < "$SCOPE_FILE"
  [ "$S_scope_version" = 1 ] || die "scope file: scope_version must be 1"
  [[ "$S_valid_from" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && [[ "$S_valid_until" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "scope file: valid_from/valid_until must be YYYY-MM-DD"
  TODAY="$(date -u +%Y-%m-%d)"
  { [ "$TODAY" = "$S_valid_from" ] || [[ "$TODAY" > "$S_valid_from" ]]; } && { [ "$TODAY" = "$S_valid_until" ] || [[ "$TODAY" < "$S_valid_until" ]]; } \
    || die "scope file: today (UTC $TODAY) is outside the authorized window $S_valid_from .. $S_valid_until"
  [ "$S_script_sha256" = "$SELF_SHA" ] || die "scope file: script_sha256 does not match this script ($SELF_SHA)"
  [ "$S_entitlements_sha256" = "$ENT_SHA" ] || die "scope file: entitlements_sha256 does not match the entitlements file ($ENT_SHA)"
  [ "$S_input_dmg_sha256" = "$EXPECTED_SHA" ] || die "scope file: input_dmg_sha256 does not match --expected-sha256"
  [ "$S_identity_sha256" = "$(sha_str "$IDENTITY")" ] || die "scope file: identity_sha256 does not match the identity in R0P_CODESIGN_IDENTITY (the authorization names a different identity)"
  [[ "$S_team_id" =~ ^[A-Z0-9]{10}$ ]] || die "scope file: team_id must be 10 characters [A-Z0-9]"
  case "$S_notarize:$S_upload_to" in yes:apple-notary|no:none) ;; *) die "scope file: notarize/upload_to must be yes/apple-notary or no/none";; esac
  if [ "$DO_NOTARIZE" = 1 ]; then [ "$S_notarize" = yes ] || die "--notarize given but the scope file says notarize=$S_notarize"
  else [ "$S_notarize" = no ] || die "scope file authorizes notarization (notarize=yes) but --notarize was not given: run exactly what was authorized"; fi
fi

# ---------------------------------------------------------------- paths: input must be a copy, output must be new
INPUT_DMG="$(abspath "$INPUT_DMG")"
OUT_DIR="$(abspath "$OUT_DIR")" || die "parent of --out-dir does not exist"
case "$OUT_DIR" in /|/System|/System/*|/usr|/usr/*|/bin|/bin/*|/sbin|/sbin/*|/Library|/Library/*|/Applications|/Applications/*|"$HOME"|"$HOME/") die "unsafe --out-dir: $OUT_DIR";; esac
IN_DIR="$(dirname "$INPUT_DMG")"
case "$IN_DIR/" in "$OUT_DIR"/*) die "--out-dir must not contain the input dmg";; esac
candidate_like() { # dir -> 0 if the folder looks like a candidate / evidence set
  local d="$1" comp
  case "/$d/" in
    */macos-arm64-unsigned*|*/evidence-macos-arm64*|*/candidate/*|*/candidate-record/*) return 0;;
  esac
  for comp in README-LOCAL-BUILD.md build-manifest.json release-manifest.json SHA256SUMS.candidate SIGNING_REQUEST.md; do
    [ ! -e "$d/$comp" ] || return 0
  done
  return 1
}
if candidate_like "$IN_DIR"; then die "the input dmg is inside (or beside the files of) a candidate/evidence directory: $IN_DIR. Work on a COPY in a fresh signing directory (cp -p, then verify the sha256); this script never operates on the candidate in place"; fi
if candidate_like "$OUT_DIR"; then die "--out-dir looks like a candidate/evidence directory; use a fresh directory"; fi
for p in ${PROTECTED[@]+"${PROTECTED[@]}"}; do
  [ -d "$p" ] || die "--protected-dir is not a directory: $p"
  p="$(cd "$p" && pwd -P)"
  case "$INPUT_DMG" in "$p"/*) die "the input dmg is inside the protected directory $p (copies only)";; esac
  case "$OUT_DIR/" in "$p"/*) die "--out-dir is inside the protected directory $p";; esac
done
if [ -e "$OUT_DIR" ]; then [ -d "$OUT_DIR" ] && [ -z "$(ls -A "$OUT_DIR")" ] || die "--out-dir exists and is not empty: $OUT_DIR"; fi

# ---------------------------------------------------------------- tools (system locations in --real)
TOOLS="codesign hdiutil ditto plutil shasum xattr lipo diskutil spctl"
[ "$MODE" != real ] || [ "$DO_NOTARIZE" = 0 ] || TOOLS="$TOOLS xcrun"
NONSYSTEM=0
tool_path() { command -v "$1" 2>/dev/null || true; }
if [ "$PLAN" = 0 ]; then
  for t in $TOOLS; do
    tp="$(tool_path "$t")"
    [ -n "$tp" ] || die "required tool not found: $t"
    if [ "$MODE" = real ]; then
      case "$tp" in
        /usr/bin/*|/bin/*|/usr/sbin/*|/sbin/*|/Library/Developer/CommandLineTools/usr/bin/*|/Applications/Xcode*.app/Contents/Developer/*) ;;
        *) if [ "${R0P_ALLOW_NONSYSTEM_TOOLS:-}" = "stub-test" ]; then NONSYSTEM=1; else die "in --real the tool $t must resolve to a system location (got $tp)"; fi;;
      esac
    fi
  done
fi

# ---------------------------------------------------------------- input verification (a read of the file)
ACTUAL_SHA="$(sha "$INPUT_DMG")"
[ "$ACTUAL_SHA" = "$EXPECTED_SHA" ] || die "input dmg sha256 mismatch: actual $ACTUAL_SHA, expected $EXPECTED_SHA (nothing was created)"

# ---------------------------------------------------------------- output scaffolding
WORK="$OUT_DIR/work"; STAGE="$WORK/stage"; TRANSCRIPT=""
if [ "$PLAN" = 0 ]; then
  mkdir -p "$WORK/tmp" "$STAGE" "$OUT_DIR/notary" "$OUT_DIR/codesign" "$OUT_DIR/verify"
  chmod 700 "$OUT_DIR" "$WORK"
  export TMPDIR="$WORK/tmp"
  TRANSCRIPT="$OUT_DIR/TRANSCRIPT.txt"; : >"$TRANSCRIPT"
fi
EVIDENCE="$OUT_DIR/EVIDENCE.txt"
FINISHED=0; MOUNTS=(); FAIL_NOTE=""

out() { printf '%s\n' "$*"; if [ -n "$TRANSCRIPT" ]; then printf '%s\n' "$*" >>"$TRANSCRIPT"; fi; return 0; }
say() { out "== $*"; }
ev() { if [ "$PLAN" = 0 ]; then printf '%s\n' "$*" >>"$EVIDENCE"; fi; return 0; }
evblock() { if [ "$PLAN" = 0 ]; then { printf -- '--- %s\n' "$1"; shift; "$@" 2>&1 || true; echo; } >>"$EVIDENCE"; fi; return 0; }
fail() { FAIL_NOTE="$*"; out "ERROR: $*"; exit 1; }
qcmd() { local s="" a; for a in "$@"; do s="$s$(printf '%q' "$a") "; done; printf '%s' "${s% }"; }
# run: print the command (it is a plan line in --dry-run), execute it otherwise, append its output to the transcript
run() {
  out "+ $(qcmd "$@")"
  if [ "$PLAN" = 1 ]; then return 0; fi
  local tmp="$WORK/tmp/run.out" rc=0
  "$@" >"$tmp" 2>&1 || rc=$?
  if [ -s "$tmp" ]; then sed 's/^/    | /' "$tmp" | while IFS= read -r l; do out "$l"; done; fi
  rm -f "$tmp"
  return "$rc"
}
# capture FILE cmd...: stdout of the command goes to FILE, stderr to FILE.stderr (both kept as evidence)
capture() {
  local f="$1"; shift
  out "+ $(qcmd "$@") > $(basename "$f")"
  if [ "$PLAN" = 1 ]; then return 0; fi
  local rc=0
  "$@" >"$f" 2>"$f.stderr" || rc=$?
  return "$rc"
}

cleanup() {
  local rc=$? m
  set +e
  for m in ${MOUNTS[@]+"${MOUNTS[@]}"}; do [ -n "$m" ] && hdiutil detach "$m" -quiet 2>/dev/null; done
  if [ "$PLAN" = 0 ] && [ "$FINISHED" != 1 ] && [ -d "$OUT_DIR" ]; then
    { echo "FAILED (exit status $rc) at $(date -u +%Y-%m-%dT%H:%M:%SZ) by $(basename "$SELF") version $SCRIPT_VERSION (mode $MODE)."
      [ -z "$FAIL_NOTE" ] || echo "reason: $FAIL_NOTE"
      echo "work/ and any partial output were left in place for diagnosis; use a NEW --out-dir for the next attempt."
      echo "A failed run is never reused. In particular a failed or timed-out notarization submission is not re-uploaded without the owner's OK."
      echo "Nothing in this directory is a finished, verified result."; } >"$OUT_DIR/FAILED.txt" 2>/dev/null
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'FAIL_NOTE="interrupted by a signal"; exit 130' INT TERM HUP

purge_tree() { # remove a directory tree file by file (named rm -f, then rmdir deepest-first); never rm -rf
  local d="$1" p
  [ -d "$d" ] || return 0
  case "$d" in "$OUT_DIR"/*) ;; *) echo "purge_tree: refusing path outside out-dir: $d" >&2; return 1;; esac
  find "$d" -depth \( -type f -o -type l \) -print0 | while IFS= read -r -d '' p; do rm -f "$p"; done
  find "$d" -depth -type d -print0 | while IFS= read -r -d '' p; do rmdir "$p" 2>/dev/null || true; done
  [ ! -e "$d" ]
}
within() { case "$(abspath "$1")" in "$OUT_DIR"/*) return 0;; *) return 1;; esac; }
attach() { # image mountpoint [extra hdiutil flags...]
  local img="$1" mp="$2"; shift 2
  within "$mp" || fail "mountpoint outside out-dir: $mp"
  mkdir -p "$mp"
  out "+ hdiutil attach $* -nobrowse -noverify -noautoopen -mountpoint $mp $img"
  hdiutil attach "$@" -nobrowse -noverify -noautoopen -mountpoint "$mp" "$img" >/dev/null || fail "hdiutil attach failed for $img (if 'Resource busy', detach the image elsewhere and use a new --out-dir)"
  MOUNTS+=("$mp")
}
detach() { run hdiutil detach "$1" -quiet; }

# ---------------------------------------------------------------- banner / plan header
case "$MODE" in
  dry-run) MODE_TEXT="DRY-RUN (default): nothing is created, signed, mounted or uploaded; commands are printed only";;
  adhoc-test) MODE_TEXT="ADHOC-TEST: ad-hoc identity '-', no timestamp, NO notarization, NO stapling; output is ADHOC-TEST-ONLY_*";;
  real) MODE_TEXT="REAL signing (identity from the environment; sha256(identity)=$(sha_str "$IDENTITY")); $( [ "$DO_NOTARIZE" = 1 ] && echo "notarize + staple with a keychain profile; the signed dmg IS UPLOADED to the Apple notary service" || echo "sign only: no upload")";;
esac
OUT_DMG_NAME="$DMG_NAME"; [ "$MODE" = adhoc-test ] && OUT_DMG_NAME="ADHOC-TEST-ONLY_$DMG_NAME"
OUT_DMG="$OUT_DIR/$OUT_DMG_NAME"
say "$SCRIPT_VERSION  mode: $MODE_TEXT"
out "line:     $RELEASE_LINE  product=$PRODUCT version=$VERSION bundle id=$BUNDLE_ID"
out "expected: app=$APP_NAME launcher=$MAIN_EXE sidecar=$SIDECAR_EXE sidecar identifier=$SIDECAR_ID dmg=$DMG_NAME volume=$VOLNAME"
out "script:   sha256=$SELF_SHA"
out "plist:    $ENTITLEMENTS sha256=$ENT_SHA"
out "input:    $INPUT_DMG sha256=$ACTUAL_SHA (verified equal to --expected-sha256; input basename is not required to match $DMG_NAME)"
out "out-dir:  $OUT_DIR"
if [ "$PLAN" = 1 ]; then
  out "DRY-RUN: the sha256 of the input was verified (a read). The commands below are the --real --notarize sequence, printed and NOT executed;"
  out "         --adhoc-test runs the same sequence with identity '-', --timestamp=none and without notarization, on a copy, with no secret."
fi

if [ "$PLAN" = 0 ]; then
  : >"$EVIDENCE"
  ev "Agentic Apps R0-prime $VERSION (macOS arm64) -- signing evidence (script $SCRIPT_VERSION)"
  ev "generated:  $(date -u +%Y-%m-%dT%H:%M:%SZ) by $(basename "$SELF") sha256=$SELF_SHA"
  ev "MODE:       $MODE_TEXT"
  ev "host:       $(sw_vers -productName) $(sw_vers -productVersion) ($(sw_vers -buildVersion)) $(uname -m); CLT: $(pkgutil --pkg-info com.apple.pkg.CLTools_Executables 2>/dev/null | awk '/^version:/{print $2}')"
  ev "entitlements file sha256=$ENT_SHA (name: $(basename "$ENTITLEMENTS"))"
  ev "identity.json sha256=$(sha "$IDJSON")"
  for t in $TOOLS; do ev "tool $t: $(tool_path "$t")"; done
  if [ "$NONSYSTEM" = 1 ]; then ev "*** NON-SYSTEM TOOLS (R0P_ALLOW_NONSYSTEM_TOOLS=stub-test): this is a stub/test run, NOT a signing run ***"; fi
  if [ "$MODE" = adhoc-test ]; then
    ev ""; ev "*** AD-HOC TEST. The ad-hoc signature is NOT a Developer ID signature: no Apple timestamp, no Team ID, nothing was sent to Apple,"
    ev "*** nothing was stapled, Gatekeeper (spctl) is EXPECTED to reject the result, and the output must never be distributed. ***"
    printf '%s\n' "AD-HOC TEST OUTPUT. Not a release artifact. Signed with the ad-hoc identity on a copy; never distribute." >"$OUT_DIR/ADHOC-TEST-NOT-A-RELEASE.txt"
  fi
  if [ "$MODE" = real ]; then
    ev "authorization: scope file sha256=$AUTH_SHA (valid $S_valid_from .. $S_valid_until UTC); team_id=$S_team_id; notarize=$S_notarize; upload_to=$S_upload_to"
    ev "identity: only sha256(identity)=$(sha_str "$IDENTITY") is recorded; the profile name is not recorded either"
  fi
  ev ""; ev "INPUT dmg: $(basename "$INPUT_DMG")  sha256 actual=$ACTUAL_SHA expected=$EXPECTED_SHA  RESULT: match  size=$(stat -f %z "$INPUT_DMG")"
fi

# ---------------------------------------------------------------- 2. mount a private copy read-only, stage the app
say "step 1: private copy of the input, mount read-only, stage the app"
IN_MNT="$WORK/in-mnt"; IN_COPY="$WORK/input-copy.dmg"
APP="$STAGE/$APP_NAME"; MACOS="$APP/Contents/MacOS"; RES="$APP/Contents/Resources"
run cp -p "$INPUT_DMG" "$IN_COPY"
if [ "$PLAN" = 0 ]; then
  [ "$(sha "$IN_COPY")" = "$ACTUAL_SHA" ] || fail "private copy of the input dmg differs from the verified input (disk/IO problem?)"
  attach "$IN_COPY" "$IN_MNT" -readonly
  [ -d "$IN_MNT/$APP_NAME" ] || fail "$APP_NAME not found in the input dmg (is this an R0-prime dmg? the frozen v0.3.2 dmg holds 'Agentic Apps.app' and is refused)"
  # the rebuilt dmg holds only the app, the volume icon and the Applications link: refuse anything else the input carries
  for e in "$IN_MNT"/* "$IN_MNT"/.[!.]*; do
    [ -e "$e" ] || [ -L "$e" ] || continue
    case "$(basename "$e")" in
      "$APP_NAME"|.VolumeIcon.icns|Applications|.fseventsd|.Trashes|.DS_Store|.TemporaryItems) ;;
      *) fail "the input dmg root holds an item the rebuilt dmg would drop: $(basename "$e"). Update the script and its review first (for example a Licenses folder at the dmg root)";;
    esac
  done
fi
run ditto "$IN_MNT/$APP_NAME" "$APP"
if [ "$PLAN" = 0 ]; then
  [ -f "$IN_MNT/.VolumeIcon.icns" ] && cp -p "$IN_MNT/.VolumeIcon.icns" "$STAGE/.VolumeIcon.icns"
  IN_VOLNAME="$(diskutil info "$IN_MNT" 2>/dev/null | sed -n 's/^ *Volume Name: *//p')" || IN_VOLNAME=""
  ev "input volume name: ${IN_VOLNAME:-?}; input dmg root:"; evblock "ls -la of the input dmg root" ls -laO "$IN_MNT"
fi
detach "$IN_MNT"
run xattr -cr "$APP"

# ---------------------------------------------------------------- 3. layout checks (fail closed)
say "step 2: layout and identity checks on the staged app"
if [ "$PLAN" = 1 ]; then
  out "  (checked when executing: $MAIN_EXE and $SIDECAR_EXE are the only executables and the only Mach-O files; both are arm64-only;"
  out "   Info.plist: CFBundleIdentifier=$BUNDLE_ID, CFBundleExecutable=$MAIN_EXE, version $VERSION/$BUNDLE_VERSION, LSMinimumSystemVersion 11.0, RedevopsReleaseLine=$RELEASE_LINE;"
  out "   Resources/Licenses exists, its files are not executable; no Frameworks/PlugIns/Helpers/XPCServices/_CodeSignature)"
else
  [ -f "$MACOS/$MAIN_EXE" ] && [ -f "$MACOS/$SIDECAR_EXE" ] || fail "expected executables missing in the bundle ($MAIN_EXE, $SIDECAR_EXE)"
  # Contents/MacOS: exactly the two expected files
  NMACOS="$(find "$MACOS" -mindepth 1 | wc -l | tr -d ' ')"
  [ "$NMACOS" = 2 ] || fail "Contents/MacOS holds $NMACOS entries, expected exactly the launcher and the sidecar"
  for d in Frameworks PlugIns Helpers XPCServices _CodeSignature SharedSupport Library; do
    [ ! -e "$APP/Contents/$d" ] || fail "unexpected Contents/$d in the bundle (nested code we would not know to sign)"
  done
  # no other executables, libraries or Mach-O files anywhere in the bundle
  EXTRA="$(find "$APP" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) ! -path "$MACOS/$MAIN_EXE" ! -path "$MACOS/$SIDECAR_EXE" | head -5)"
  [ -z "$EXTRA" ] || fail "unexpected executable/library files in the bundle (add them to the signing order first): $EXTRA"
  while IFS= read -r -d '' f; do
    case "$f" in "$MACOS/$MAIN_EXE"|"$MACOS/$SIDECAR_EXE") continue;; esac
    magic="$(head -c 4 "$f" | od -An -tx1 | tr -d ' \n')"
    case "$magic" in feedface|feedfacf|cefaedfe|cffaedfe|cafebabe|bebafeca) fail "Mach-O file not in the signing plan: ${f#"$APP"/}";; esac
  done < <(find "$APP" -type f -print0)
  for b in "$MAIN_EXE" "$SIDECAR_EXE"; do
    ARCHS="$(lipo -archs "$MACOS/$b" 2>/dev/null)" || fail "lipo -archs failed for $b"
    [ "$ARCHS" = arm64 ] || fail "$b is not arm64-only (lipo says: $ARCHS)"
  done
  PL="$APP/Contents/Info.plist"
  [ "$(plutil -extract CFBundleIdentifier raw -o - "$PL" 2>/dev/null)" = "$BUNDLE_ID" ] || fail "Info.plist CFBundleIdentifier is not $BUNDLE_ID"
  [ "$(plutil -extract CFBundleExecutable raw -o - "$PL" 2>/dev/null)" = "$MAIN_EXE" ] || fail "Info.plist CFBundleExecutable is not $MAIN_EXE"
  [ "$(plutil -extract CFBundleShortVersionString raw -o - "$PL" 2>/dev/null)" = "$VERSION" ] || fail "Info.plist CFBundleShortVersionString is not $VERSION"
  [ "$(plutil -extract CFBundleVersion raw -o - "$PL" 2>/dev/null)" = "$BUNDLE_VERSION" ] || fail "Info.plist CFBundleVersion is not $BUNDLE_VERSION"
  [ "$(plutil -extract LSMinimumSystemVersion raw -o - "$PL" 2>/dev/null)" = "11.0" ] || fail "Info.plist LSMinimumSystemVersion is not 11.0"
  [ "$(plutil -extract RedevopsReleaseLine raw -o - "$PL" 2>/dev/null)" = "$RELEASE_LINE" ] || fail "Info.plist RedevopsReleaseLine is not $RELEASE_LINE"
  [ -d "$RES/Licenses" ] || fail "Contents/Resources/Licenses is missing"
  NLIC="$(find "$RES/Licenses" -type f | wc -l | tr -d ' ')"
  [ "$NLIC" -ge 1 ] || fail "Contents/Resources/Licenses holds no files"
  [ -z "$(find "$RES/Licenses" -type f -perm -u+x | head -1)" ] || fail "a file in Resources/Licenses is executable"
  ev ""; ev "INPUT binaries (before signing; ad-hoc/linker-signed by the build):"
  ev "  $MAIN_EXE sha256=$(sha "$MACOS/$MAIN_EXE")"; ev "  $SIDECAR_EXE sha256=$(sha "$MACOS/$SIDECAR_EXE")"
  ev "  Info.plist sha256=$(sha "$PL"); Licenses files: $NLIC"
  evblock "Info.plist keys" bash -c "plutil -p '$PL' | grep -E 'CFBundleIdentifier|CFBundleShortVersionString|CFBundleVersion|LSMinimumSystemVersion|CFBundleExecutable|RedevopsReleaseLine'"
  out "  layout OK: 2 executables (arm64), $NLIC licence files, identity matches identity.json"
fi

# ---------------------------------------------------------------- 4. sign inside-out
say "step 3: sign inside-out (sidecar, launcher, bundle); hardened runtime; $( [ "$MODE" = adhoc-test ] && echo 'no timestamp (ad-hoc)' || echo 'secure timestamp')"
# csign OPTIONS... PATH: codesign with the current identity. In --real and in the plan the transcript shows a placeholder, never the identity.
csign() {
  local shown="$IDENTITY"
  if [ "$MODE" = real ] || [ "$PLAN" = 1 ]; then shown="<identity from R0P_CODESIGN_IDENTITY>"; fi
  out "+ codesign -s $shown --force $TS_FLAG $(qcmd "$@")"
  if [ "$PLAN" = 1 ]; then return 0; fi
  local rc=0
  codesign -s "$IDENTITY" --force "$TS_FLAG" "$@" >"$WORK/tmp/cs.out" 2>&1 || rc=$?
  if [ -s "$WORK/tmp/cs.out" ]; then sed 's/^/    | /' "$WORK/tmp/cs.out" | while IFS= read -r l; do out "$l"; done; fi
  return "$rc"
}
cs() { csign --options runtime "$@"; }
cs --identifier "$SIDECAR_ID" --entitlements "$ENTITLEMENTS" "$MACOS/$SIDECAR_EXE"
cs "$MACOS/$MAIN_EXE"
cs "$APP"      # bundle last: seals Info.plist + Resources (including Licenses/); re-signs the main executable with the bundle id

# ---------------------------------------------------------------- 5. verify the signatures (per binary and bundle; no --deep)
say "step 4: verify (codesign --verify --strict per binary and for the bundle; -dvvv; entitlements; hardened runtime)"
ENT_READER=""; ENT_JSON=""
read_entitlements() { # FILE LABEL -> ENT_JSON; FAILS CLOSED
  local f="$1" label="$2" base="$OUT_DIR/codesign/$2.entitlements" rc=0 xml hr
  xml="$base.plist"; hr="$base.human.txt"
  ENT_READER="primary (--entitlements - --xml)"
  codesign -d --entitlements - --xml "$f" >"$xml" 2>"$base.stderr.txt" || rc=$?
  if [ "$rc" -ne 0 ]; then
    rc=0; ENT_READER="fallback (--entitlements :-)"
    codesign -d --entitlements :- "$f" >"$xml" 2>"$base.stderr.txt" || rc=$?
  fi
  [ "$rc" -eq 0 ] || fail "$label: cannot read the entitlements (codesign -d --entitlements failed with status $rc); refusing to assume none"
  grep -q '^Executable=' "$base.stderr.txt" || fail "$label: codesign gave no 'Executable=' report while reading the entitlements; refusing to assume none"
  rc=0
  codesign -d --entitlements - "$f" >"$hr" 2>>"$base.stderr.txt" || rc=$?
  [ "$rc" -eq 0 ] || fail "$label: cannot read the entitlements (human-readable dump failed with status $rc); refusing to assume none"
  if [ -s "$xml" ]; then
    ENT_JSON="$(plutil -convert json -o - "$xml" 2>/dev/null)" || fail "$label: entitlement data is not a parsable plist; refusing to assume none"
    [ -n "$ENT_JSON" ] || fail "$label: entitlement data parsed to nothing; refusing to assume none"
    grep -q '\[Dict\]' "$hr" || fail "$label: the two entitlement dumps disagree (XML has data, human-readable dump has none)"
  else
    ENT_JSON="{}"
    if grep -q '\[Dict\]' "$hr" || [ -s "$hr" ]; then fail "$label: the two entitlement dumps disagree (XML empty, human-readable dump not empty)"; fi
  fi
}
TEAM_SEEN=""
check_bin() { # path label want_entitlements(0/1) want_identifier
  local f="$1" label="$2" ent="$3" wantid="$4" out_f="$OUT_DIR/codesign/$2.dvvv.txt" entj id team
  out "+ codesign --verify --strict --verbose=2 <$label>"
  codesign --verify --strict --verbose=2 "$f" >"$OUT_DIR/codesign/$label.verify.txt" 2>&1 || { cat "$OUT_DIR/codesign/$label.verify.txt" >&2; fail "codesign --verify --strict failed for $label"; }
  out "+ codesign -dvvv <$label>"
  codesign -dvvv "$f" >"$out_f" 2>&1 || fail "codesign -dvvv failed for $label"
  grep -Eq 'flags=0x[0-9a-f]+\([^)]*runtime[^)]*\)' "$out_f" || fail "hardened runtime flag missing on $label"
  id="$(sed -n 's/^Identifier=//p' "$out_f" | head -1)"
  [ "$id" = "$wantid" ] || fail "$label has identifier '$id', expected '$wantid'"
  if [ "$MODE" = real ]; then
    grep -q '^Authority=Developer ID Application:' "$out_f" || fail "$label is not signed by a Developer ID Application authority"
    grep -q '^Authority=Developer ID Certification Authority' "$out_f" || fail "$label: the Developer ID Certification Authority is not in the chain"
    grep -q '^Authority=Apple Root CA' "$out_f" || fail "$label: Apple Root CA is not in the chain"
    grep -q '^Timestamp=' "$out_f" || fail "$label has no secure timestamp"
    team="$(sed -n 's/^TeamIdentifier=//p' "$out_f" | head -1)"
    [ -n "$team" ] && [ "$team" != "not set" ] || fail "$label has no Team ID"
    [ "$team" = "$S_team_id" ] || fail "$label Team ID differs from the authorized team_id"
  fi
  read_entitlements "$f" "$label"; entj="$ENT_JSON"
  case "$entj" in *get-task-allow*) fail "$label carries get-task-allow (debug entitlement); notarization would fail";; esac
  if [ "$ent" = 1 ]; then
    [ "$entj" = "$EXPECTED_ENT_JSON" ] || fail "$label entitlements differ from the expected set: $entj"
  else
    [ "$entj" = "{}" ] || fail "$label unexpectedly carries entitlements: $entj"
  fi
  ev "  $label: hardened runtime PRESENT; identifier=$id; entitlements = $entj [read via $ENT_READER]; cdhash=$(sed -n 's/^CDHash=//p' "$out_f"); $(grep -E '^(TeamIdentifier|Timestamp|Signature)' "$out_f" | head -3 | tr '\n' ' ')"
  out "  $label: OK (runtime, identifier $id, entitlements $entj)"
}
if [ "$PLAN" = 1 ]; then
  out "  (executed in --adhoc-test and --real: sidecar identifier=$SIDECAR_ID with entitlements exactly $EXPECTED_ENT_JSON; launcher and bundle identifier=$BUNDLE_ID with NO entitlements;"
  out "   hardened-runtime flag on each; no get-task-allow; in --real also Developer ID authority chain, secure timestamp, one Team ID equal to the scope file's)"
else
  ev ""; ev "SIGNED binaries (checks: runtime flag, identifier, entitlements, no get-task-allow$( [ "$MODE" = real ] && echo ', Developer ID chain, timestamp, Team ID'))"
  check_bin "$MACOS/$SIDECAR_EXE" sidecar 1 "$SIDECAR_ID"
  check_bin "$MACOS/$MAIN_EXE" launcher 0 "$BUNDLE_ID"
  codesign --verify --strict --verbose=2 "$APP" >"$OUT_DIR/codesign/bundle.verify.txt" 2>&1 || { cat "$OUT_DIR/codesign/bundle.verify.txt" >&2; fail "codesign --verify --strict failed for the bundle"; }
  codesign -dvvv "$APP" >"$OUT_DIR/codesign/bundle.dvvv.txt" 2>&1 || fail "codesign -dvvv failed for the bundle"
  BID="$(sed -n 's/^Identifier=//p' "$OUT_DIR/codesign/bundle.dvvv.txt" | head -1)"
  [ "$BID" = "$BUNDLE_ID" ] || fail "bundle identifier is '$BID', expected '$BUNDLE_ID'"
  grep -q '^Info.plist=not bound' "$OUT_DIR/codesign/bundle.dvvv.txt" && fail "the bundle signature does not bind Info.plist"
  grep -q '^Sealed Resources=none' "$OUT_DIR/codesign/bundle.dvvv.txt" && fail "the bundle signature seals no resources (Licenses/ would be unprotected)"
  SEALED="$(sed -n 's/^Sealed Resources version=[0-9]* rules=[0-9]* files=\([0-9]*\).*/\1/p' "$OUT_DIR/codesign/bundle.dvvv.txt" | head -1)"
  [ -n "$SEALED" ] || fail "could not read the sealed-resource count from codesign -dvvv"
  [ "$SEALED" -ge "$NLIC" ] || fail "the bundle seals $SEALED files but Resources/Licenses alone holds $NLIC"
  grep -Eq 'flags=0x[0-9a-f]+\([^)]*runtime[^)]*\)' "$OUT_DIR/codesign/bundle.dvvv.txt" || fail "hardened runtime flag missing on the bundle"
  ev "  bundle: identifier=$BID; sealed resource files=$SEALED (Licenses/ holds $NLIC); $(grep -E '^(Info.plist|Sealed Resources|TeamIdentifier|Timestamp|Signature)' "$OUT_DIR/codesign/bundle.dvvv.txt" | tr '\n' ' ')"
  out "  bundle: OK (identifier $BID, $SEALED sealed files >= $NLIC licence files)"
  ev ""; ev "SIGNED binary hashes: $MAIN_EXE sha256=$(sha "$MACOS/$MAIN_EXE")"; ev "                     $SIDECAR_EXE sha256=$(sha "$MACOS/$SIDECAR_EXE")"
  { echo "$(sha "$MACOS/$MAIN_EXE")  Contents/MacOS/$MAIN_EXE"; echo "$(sha "$MACOS/$SIDECAR_EXE")  Contents/MacOS/$SIDECAR_EXE"; echo "$(sha "$APP/Contents/Info.plist")  Contents/Info.plist"; } >"$OUT_DIR/codesign/SIGNED-BINARIES.sha256"
  for l in sidecar launcher bundle; do evblock "codesign -dvvv ($l)" cat "$OUT_DIR/codesign/$l.dvvv.txt"; done
fi

# ---------------------------------------------------------------- 6. build + sign the dmg
say "step 5: build the dmg (UDRW -> volume icon flag -> UDZO) and sign it"
RW="$WORK/rw.dmg"; RW_MNT="$WORK/rw-mnt"
run ln -s /Applications "$STAGE/Applications"
run hdiutil create -srcfolder "$STAGE" -volname "$VOLNAME" -fs HFS+ -format UDRW -ov "$RW"
if [ "$PLAN" = 0 ]; then
  attach "$RW" "$RW_MNT"
  if [ -f "$RW_MNT/.VolumeIcon.icns" ]; then
    if command -v SetFile >/dev/null 2>&1; then SetFile -a C "$RW_MNT" || ev "WARNING: SetFile -a C failed (volume icon flag not set)"
    else ev "WARNING: SetFile not found (Xcode CLT); volume custom-icon flag not set"; fi
  fi
  sync
else
  out "+ hdiutil attach -nobrowse -noverify -noautoopen -mountpoint $RW_MNT $RW   # then SetFile -a C <mount>"
fi
detach "$RW_MNT"
run hdiutil convert "$RW" -format UDZO -imagekey zlib-level=9 -ov -o "$OUT_DMG"
if [ "$PLAN" = 0 ]; then [ -f "$OUT_DMG" ] || fail "dmg was not produced"; fi
csign "$OUT_DMG" || fail "codesign of the dmg failed"
if [ "$PLAN" = 0 ]; then
  out "+ codesign --verify --strict --verbose=2 <dmg>"
  codesign --verify --strict --verbose=2 "$OUT_DMG" >"$OUT_DIR/codesign/dmg.verify.txt" 2>&1 || fail "dmg signature verify failed"
  codesign -dvvv "$OUT_DMG" >"$OUT_DIR/codesign/dmg.dvvv.txt" 2>&1 || true
  if [ "$MODE" = real ]; then
    grep -q '^Authority=Developer ID Application:' "$OUT_DIR/codesign/dmg.dvvv.txt" || fail "the dmg is not signed by a Developer ID Application authority"
    grep -q '^Timestamp=' "$OUT_DIR/codesign/dmg.dvvv.txt" || fail "the dmg has no secure timestamp"
    [ "$(sed -n 's/^TeamIdentifier=//p' "$OUT_DIR/codesign/dmg.dvvv.txt" | head -1)" = "$S_team_id" ] || fail "the dmg's Team ID differs from the authorized team_id"
  fi
  SIGNED_SHA="$(sha "$OUT_DMG")"
  ev ""; ev "OUTPUT dmg: $(basename "$OUT_DMG")"; ev "  sha256 (signed, before notarization/stapling): $SIGNED_SHA"
  evblock "codesign -dvvv (dmg)" cat "$OUT_DIR/codesign/dmg.dvvv.txt"
fi

# ---------------------------------------------------------------- 7. notarize + staple (--real --notarize only)
NOTARY_ID="n/a"; NOTARY_STATUS="not submitted"; STAPLE="dmg: NOT stapled; app inside the dmg: NOT stapled"
if [ "$MODE" = real ] && [ "$DO_NOTARIZE" = 1 ]; then
  say "step 6: notarize (UPLOADS the signed dmg to the Apple notary service; one submission, time-boxed to $NOTARY_TIMEOUT, no retry)"
  SUBMIT_JSON="$OUT_DIR/notary/submit.json"
  out "+ xcrun notarytool submit $OUT_DMG --keychain-profile <profile from R0P_NOTARY_PROFILE> --wait --timeout $NOTARY_TIMEOUT --output-format json > notary/submit.json"
  xcrun notarytool submit "$OUT_DMG" --keychain-profile "$NOTARY_PROFILE" --wait --timeout "$NOTARY_TIMEOUT" --output-format json >"$SUBMIT_JSON" 2>"$OUT_DIR/notary/submit.stderr.txt" || true
  NOTARY_ID="$(plutil -extract id raw -o - "$SUBMIT_JSON" 2>/dev/null || echo unknown)"
  NOTARY_STATUS="$(plutil -extract status raw -o - "$SUBMIT_JSON" 2>/dev/null || echo unknown)"
  out "  notarytool: submission id=$NOTARY_ID status=$NOTARY_STATUS"
  if [ "$NOTARY_ID" != unknown ]; then
    out "+ xcrun notarytool log $NOTARY_ID --keychain-profile <profile> notary/log.json"
    xcrun notarytool log "$NOTARY_ID" --keychain-profile "$NOTARY_PROFILE" "$OUT_DIR/notary/log.json" >/dev/null 2>&1 || ev "WARNING: could not fetch the notary log"
  fi
  ev ""; ev "NOTARY: submission id=$NOTARY_ID status=$NOTARY_STATUS (log: notary/log.json, sha256=$( [ -f "$OUT_DIR/notary/log.json" ] && sha "$OUT_DIR/notary/log.json" || echo none))"
  ev "UPLOADED to the Apple notary service: $(basename "$OUT_DMG") sha256=$SIGNED_SHA"
  if [ "$NOTARY_STATUS" != "Accepted" ]; then
    ev "RESULT: NOT ACCEPTED -- see notary/log.json; not stapled. DO NOT resubmit, re-sign or retry without the owner's explicit OK; this submission is never reused."
    fail "notarization status is '$NOTARY_STATUS' (id $NOTARY_ID); see notary/log.json. If the status is 'unknown' after a timeout, check the id with 'xcrun notarytool info $NOTARY_ID' instead of uploading again"
  fi
  say "step 7: staple the dmg + validate"
  out "+ xcrun stapler staple <dmg>"
  xcrun stapler staple "$OUT_DMG" >"$OUT_DIR/notary/staple.txt" 2>&1 || { cat "$OUT_DIR/notary/staple.txt" >&2; fail "stapler staple failed"; }
  out "+ xcrun stapler validate <dmg>"
  xcrun stapler validate "$OUT_DMG" >"$OUT_DIR/notary/staple-validate.txt" 2>&1 || { cat "$OUT_DIR/notary/staple-validate.txt" >&2; fail "stapler validate failed"; }
  STAPLE="dmg: stapled + validated (stapler validate on the dmg passed); app inside the dmg: NOT stapled (only the dmg is stapled)"
elif [ "$PLAN" = 1 ]; then
  say "step 6/7: notarize and staple (only with --real --notarize; the plan shows them)"
  out "+ xcrun notarytool submit $(printf '%q' "$OUT_DMG") --keychain-profile <profile from R0P_NOTARY_PROFILE> --wait --timeout $NOTARY_TIMEOUT --output-format json   # UPLOADS the signed dmg to Apple"
  out "+ xcrun notarytool log <submission id> --keychain-profile <profile> notary/log.json"
  out "+ xcrun stapler staple $(printf '%q' "$OUT_DMG")"
  out "+ xcrun stapler validate $(printf '%q' "$OUT_DMG")"
  out "+ xcrun stapler validate <app copied out of the dmg>   # EXPECTED: no ticket"
elif [ "$MODE" = adhoc-test ]; then
  NOTARY_STATUS="SKIPPED (adhoc test: notarytool was NOT called; nothing was uploaded to Apple)"; STAPLE="dmg: NOT stapled (adhoc test: stapler was NOT called); app inside the dmg: NOT stapled"
else
  NOTARY_STATUS="NOT SUBMITTED (--notarize not given; the scope file says notarize=no)"
fi
if [ "$PLAN" = 0 ]; then
  FINAL_SHA="$(sha "$OUT_DMG")"
  ev ""; ev "staple: $STAPLE"; ev "OUTPUT dmg sha256 (final): $FINAL_SHA  size=$(stat -f %z "$OUT_DMG")"
  [ ! -f "$OUT_DIR/notary/staple-validate.txt" ] || evblock "stapler validate" cat "$OUT_DIR/notary/staple-validate.txt"
fi

# ---------------------------------------------------------------- 8. Gatekeeper assessment of the dmg and of the app copied out
say "step 8: re-mount the output, copy the app out, verify, Gatekeeper (spctl)"
VMNT="$OUT_DIR/verify/mnt"; COPY="$OUT_DIR/verify/app/$APP_NAME"
run mkdir -p "$OUT_DIR/verify/app"
if [ "$PLAN" = 0 ]; then mkdir -p "$OUT_DIR/verify/app"; attach "$OUT_DMG" "$VMNT" -readonly; else out "+ hdiutil attach -readonly -nobrowse -noverify -noautoopen -mountpoint $VMNT $OUT_DMG"; fi
run ditto "$VMNT/$APP_NAME" "$COPY"
if [ "$PLAN" = 0 ]; then
  ev ""; evblock "output dmg root listing" ls -laO "$VMNT"
  ev "output dmg volume attributes: $(GetFileInfo "$VMNT" 2>/dev/null | sed -n 's/^attributes: //p') (capital C = custom volume icon flag set)"
  if [ -L "$VMNT/Applications" ]; then ev "Applications symlink -> $(readlink "$VMNT/Applications")"; else ev "WARNING: Applications symlink missing"; fi
fi
detach "$VMNT"
if [ "$PLAN" = 0 ]; then
  out "+ codesign --verify --strict --verbose=2 <app copied out of the output dmg>"
  codesign --verify --strict --verbose=2 "$COPY" >"$OUT_DIR/verify/app-copy.verify.txt" 2>&1 || fail "app copied out of the output dmg fails codesign verify"
  ev "codesign --verify --strict of the app copied out of the output dmg: PASSED"
  [ "$(sha "$COPY/Contents/MacOS/$MAIN_EXE")" = "$(sed -n "s#^\\([0-9a-f]*\\)  Contents/MacOS/$MAIN_EXE\$#\\1#p" "$OUT_DIR/codesign/SIGNED-BINARIES.sha256")" ] || fail "the launcher in the output dmg differs from the signed launcher"
  [ "$(sha "$COPY/Contents/MacOS/$SIDECAR_EXE")" = "$(sed -n "s#^\\([0-9a-f]*\\)  Contents/MacOS/$SIDECAR_EXE\$#\\1#p" "$OUT_DIR/codesign/SIGNED-BINARIES.sha256")" ] || fail "the sidecar in the output dmg differs from the signed sidecar"
  ev "copied-out binaries: $MAIN_EXE sha256=$(sha "$COPY/Contents/MacOS/$MAIN_EXE")  $SIDECAR_EXE sha256=$(sha "$COPY/Contents/MacOS/$SIDECAR_EXE") (equal to the signed ones)"
fi
if [ "$MODE" = real ] && [ "$DO_NOTARIZE" = 1 ]; then
  APP_STAPLE_RC=0
  out "+ xcrun stapler validate <app copied out of the dmg>   # EXPECTED: no ticket (only the dmg is stapled)"
  xcrun stapler validate "$COPY" >"$OUT_DIR/verify/app-copy.stapler-validate.txt" 2>&1 || APP_STAPLE_RC=$?
  ev "stapler validate on the app copied out of the dmg: exit=$APP_STAPLE_RC: $(tr '\n' ' ' <"$OUT_DIR/verify/app-copy.stapler-validate.txt")"
  if [ "$APP_STAPLE_RC" -ne 0 ]; then ev "EXPECTED: the app copied out of the dmg has NO stapled ticket (only the dmg is stapled)"
  else ev "the app copied out of the dmg unexpectedly carries a ticket (not stapled by this script); record only"; fi
fi
SPCTL_FAIL=0
spctl_run() { # label cmd...
  local label="$1"; shift; local o rc=0
  out "+ $(qcmd "$@")"
  if [ "$PLAN" = 1 ]; then return 0; fi
  o="$("$@" 2>&1)" || rc=$?
  ev "spctl [$label] exit=$rc: $(echo "$o" | tr '\n' ' ')"
  out "  spctl [$label] exit=$rc"
  if [ "$rc" -ne 0 ]; then SPCTL_FAIL=1; fi
  return 0
}
ev ""; ev "GATEKEEPER (spctl):"
spctl_run "dmg" spctl -a -t open --context context:primary-signature -vv "$OUT_DMG"
spctl_run "app copy" spctl -a -vv "$COPY"
if [ "$PLAN" = 0 ]; then
  if [ "$MODE" = adhoc-test ]; then
    if [ "$SPCTL_FAIL" = 1 ]; then ev "EXPECTED: Gatekeeper rejects the ad-hoc test build (no Developer ID, not notarized)."
    else ev "UNEXPECTED: Gatekeeper ACCEPTED an ad-hoc test build -- investigate before trusting this host's assessment."; fi
  elif [ "$DO_NOTARIZE" = 1 ]; then
    [ "$SPCTL_FAIL" = 0 ] || { ev "RESULT: spctl REJECTED the notarized build"; fail "spctl rejected the final dmg or app"; }
    ev "RESULT: Gatekeeper (spctl) accepts the notarized dmg, which is STAPLED, and accepts the app copied out of it."
    ev "  The app itself is NOT stapled: spctl accepted the app copy via the ONLINE notarization-ticket lookup (needs network)."
    ev "  Offline first launch of an app copied out of the dmg is UNVERIFIED until the offline-copy test of the procedure is done."
  else
    ev "NOTE: signed but not notarized (--notarize not given); spctl rejection of an un-notarized Developer ID build is expected."
  fi
  if [ "$MODE" = adhoc-test ]; then ev ""; ev "WHAT IS STAPLED: NOTHING (adhoc test). Nothing was notarized, so there is no ticket anywhere."
  elif [ "$DO_NOTARIZE" = 1 ]; then ev ""; ev "WHAT IS STAPLED: ONLY the dmg. The .app inside the dmg carries no ticket; an app copied out of the dmg relies on Gatekeeper's online lookup."
  else ev ""; ev "WHAT IS STAPLED: NOTHING (signed only; --notarize not given)."; fi
  ev ""; ev "SUMMARY: mode=[$MODE_TEXT]"
  ev "  input dmg sha256   = $ACTUAL_SHA"
  ev "  output dmg sha256  = $FINAL_SHA"
  ev "  notary id / status = $NOTARY_ID / $NOTARY_STATUS"
  ev "  staple             = $STAPLE"
  printf '%s  %s\n' "$FINAL_SHA" "$OUT_DMG_NAME" >"$OUT_DIR/OUTPUT.sha256"
fi

# ---------------------------------------------------------------- 9. tidy (success only) and the transcript hash
if [ "$PLAN" = 1 ]; then
  say "dry-run complete: nothing was created, signed, mounted or uploaded."
  exit 0
fi
say "tidy out-dir (remove staged app copy, rw.dmg, input copy, verify/app)"
for d in "$VMNT" "$IN_MNT" "$RW_MNT"; do if [ -d "$d" ]; then rmdir "$d" 2>/dev/null || true; fi; done
purge_tree "$STAGE" || ev "WARNING: could not remove the staged app copy"
purge_tree "$OUT_DIR/verify/app" || ev "WARNING: could not remove verify/app"
for f in "$RW" "$IN_COPY"; do rm -f "$f"; done
purge_tree "$WORK/tmp" || true
rmdir "$OUT_DIR/verify/mnt" 2>/dev/null || true
rmdir "$OUT_DIR/notary" 2>/dev/null || true
rmdir "$WORK" 2>/dev/null || ev "WARNING: $WORK not empty and left in place"
ev ""; ev "OUT-DIR CONTENT AFTER TIDY: the dmg, EVIDENCE.txt, TRANSCRIPT.txt(+.sha256), OUTPUT.sha256, codesign/, notary/, verify/*.txt. Removed: staged app copy, rw.dmg, input copy, app copied out of the output dmg."
FINISHED=1
say "done. Evidence: $EVIDENCE"
out "output dmg: $OUT_DMG"; out "sha256: $FINAL_SHA"
# the transcript is complete here; its hash is written beside it (a file cannot contain its own hash) and printed
TSHA="$(sha "$TRANSCRIPT")"
printf '%s  TRANSCRIPT.txt\n' "$TSHA" >"$OUT_DIR/TRANSCRIPT.sha256"
echo "transcript sha256: $TSHA"
