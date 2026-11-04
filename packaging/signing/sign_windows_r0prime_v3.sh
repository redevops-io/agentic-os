#!/usr/bin/env bash
# sign_windows_r0prime_v3.sh   (script version r0prime-win-3)
#
# Authenticode-sign the Windows files of "Agentic Apps R0-prime" from macOS with Jsign 7.6 against Azure Artifact Signing
# (Public Trust, 3-day certificates), then verify and write EVIDENCE.txt. Derived from the frozen sign_windows_artifact_signing_v2.sh
# (v0.3.2 line; independently reviewed) which is read-only and unchanged; every difference is in packaging/signing/DIFF-vs-v2.md.
# Procedure, hand-off between the Windows build host and this signing host, gates and risks:
# packaging/signing/SIGNING-PROCEDURE-R0PRIME.md.
#
# STATUS: THE REAL PATH HAS NEVER BEEN RUN. The frozen v2 production path was not run either (only its v1 predecessor ran once
# against Azure on 2026-11-04). This script was tested with stubs and, where tools exist, with a THROWAWAY key. No Azure call, no
# token, no `az` is made in any test. Signing for real needs the owner's separate, explicit authorization.
#
# TWO STAGES (the installer containers must be rebuilt after the inner executables are signed, on the Windows build host)
#   --stage inner      files the bundler packs INTO the installers (launcher, sidecar, NSIS uninstaller), named by the hand-off helper
#                      packaging/signing/r0prime_win_signcommand.sh as <role>-<sha256 of the unsigned file>.exe, role in
#                      launcher | sidecar | uninstaller. Third-party DLLs (NSIS plugins, WiX extensions) are NEVER signed with this identity.
#   --stage container  the two installers, named exactly as derived from packaging/identity.json (MSI and NSIS setup exe), signed LAST,
#                      after they were rebuilt from the signed inner files.
#
# MODES (exactly one; default --dry-run)
#   --dry-run          DEFAULT. Validates arguments, the stage, the file names and the sha256 of every input (reads only), and PRINTS
#                      the commands. No tool is started, no token is requested, nothing is created.
#   --throwaway-test   Signs COPIES with a THROWAWAY local keystore (script/make_throwaway_cert.sh of the frozen set): no Azure, no
#                      Microsoft contact; the signatures are NOT trusted by Windows. This is v2's --dry-run.
#   --real             Real signing with Azure Artifact Signing. Needs ALL of:
#                        --i-have-owner-authorization <sha256 of the scope file> and --scope-file FILE (names the stage, the input hashes,
#                        the service coordinates, the date window and this script's sha256)
#                        environment R0P_AZ_ENDPOINT, R0P_AZ_ACCOUNT, R0P_AZ_PROFILE, R0P_AZ_EXPECTED_CN (CN=<exact common name>),
#                        supplied by the owner at run time; `az` already logged in by the owner as an identity that holds the
#                        'Artifact Signing Certificate Profile Signer' role. The token is written by `az` into a 0600 file and read by
#                        Jsign with --storepass file:...; it is never printed, never on a command line, never in the environment.
#
# SCOPE FILE (scope_version=2; KEY=VALUE; read ONCE into a private copy that is hashed and parsed; a TRIPWIRE against mistakes, NOT an
# authentication: the owner's own terminal and `az` login are the authentication): not_before/not_after (UTC, at most 24 h), script_sha256,
# common_sha256, verifier_sha256, identity_json_sha256, stage, inputs_sha256 (sorted, comma separated), az_endpoint, az_account, az_profile,
# expected_cn, gate_sha256, gate_tool_sha256, distribution=yes|no (must agree with --distribution), evidence_manifest_sha256, build_commit,
# procedure_commit, tool_az_sha256, tool_java_sha256, tool_python_sha256, tool_osslsigncode_sha256 (or none), container_listing_sha256 (stage
# container: the file list of the rebuilt installers, passed with --container-listing; none for stage inner). --ledger FILE is required (one-time
# use of a scope; --allow-reuse is recorded). Tools resolve from a fixed system PATH plus the owner's R0P_TOOLS_DIR and are pinned by hash.
# The files are re-hashed right after they are copied into the private work directory and again right before each Jsign call.
#
# SAFETY (kept from v2): service coordinates are validated before anything else (endpoint ^https://[a-z0-9]+\.codesigning\.azure\.net/?$;
# account/profile ^[A-Za-z0-9-]{3,63}$); inputs are hash-verified and never modified; signed files appear in --outdir only after
# verification; a failed run leaves only a FAILED marker; INT/TERM/HUP clean up; no rm -rf; no `set -x`. New in v3: scrubbed environment
# (env -i allowlist), name policy per stage (frozen names are refused), mandatory secure timestamp in real mode, watchdog
# (R0P_SIGN_TIMEOUT_SECONDS, default 900), authorization scope binding.
#
# bash 3.2 compatible (macOS default).
#
# Usage:
#   sign_windows_r0prime_v3.sh --stage inner|container --input FILE --sha256 HEX [--input FILE2 --sha256 HEX2 ...] --outdir NEWDIR
#        [--dry-run | --throwaway-test | --real --i-have-owner-authorization SHA --scope-file FILE]
#        [--ledger FILE] [--allow-reuse] [--distribution] [--container-listing FILE]
#        [--name "Application name"] [--url URL] [--throwaway-dir DIR] [--tsa-url LOCAL_URL]
#        [--tools-dir DIR] [--ms-root-pem FILE] [--python FILE] [--verifier FILE]
case "$-" in *x*) echo "ERROR: xtrace is on; refusing (it could print secrets)" >&2; exit 1;; esac
set +x
set -euo pipefail
umask 077

SCRIPT_VERSION="r0prime-win-3.1"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
SELF_DIR="$(dirname "$SELF")"
SELF_NAME="$(basename "$SELF")"
FAILED_MAGIC="FAILED-ATTEMPT sign_windows_r0prime_v3"


usage() { sed -n '/^# Usage:/,/^case /p' "$SELF" | sed '$d' | sed 's/^# \{0,1\}//'; }
FAIL_REASON=""
die() { FAIL_REASON="$*"; echo "ERROR: $*" >&2; exit 2; }
# shared safety functions (their sha256 is bound by the scope file); the environment must BE the allowlist: no variable can skip this
. "$SELF_DIR/signing-common.sh"
r0p_scrub " R0P_AZ_ENDPOINT R0P_AZ_ACCOUNT R0P_AZ_PROFILE R0P_AZ_EXPECTED_CN R0P_SIGN_TIMEOUT_SECONDS R0P_ALLOW_NONSYSTEM_TOOLS R0P_TOOLS_DIR " "$@"
unset R0P_REEXEC
r0p_fix_path "${R0P_TOOLS_DIR:-}"      # fixed system PATH + the owner's validated tools directory (stub-test: inherited PATH, unit tests only)
export LC_ALL=C
COMMON_SHA="$(r0p_sha "$SELF_DIR/signing-common.sh")"
log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }
qcmd() { local s="" a; for a in "$@"; do s="$s$(printf '%q' "$a") "; done; printf '%s' "${s% }"; }

# ---- identity of the line (packaging/identity.json; same derivation as packaging/tools/identity.py)
IDJSON="$SELF_DIR/../identity.json"
[ -f "$IDJSON" ] || die "packaging/identity.json not found next to the script"
idj() { plutil -extract "$1" raw -o - "$IDJSON" 2>/dev/null; }
PRODUCT="$(idj r0prime.product_name)"; VERSION="$(idj r0prime.version)"
MAIN_NAME="$(idj r0prime.main_binary_name)"; SIDECAR_NAME="$(idj r0prime.sidecar_binary_name)"
FROZEN_PRODUCT="$(idj frozen.product_name)"; FROZEN_MAIN="$(idj frozen.main_binary_name)"
[[ "$PRODUCT" =~ ^[A-Za-z0-9][A-Za-z0-9\ ._-]{0,63}$ ]] && [[ "$VERSION" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] || die "identity.json: unexpected product_name/version"
[[ "$MAIN_NAME" =~ ^[A-Za-z0-9._-]{1,40}$ ]] && [[ "$SIDECAR_NAME" =~ ^[A-Za-z0-9._-]{1,40}$ ]] || die "identity.json: unexpected binary names"
[ "$PRODUCT" != "$FROZEN_PRODUCT" ] && [ "$MAIN_NAME" != "$FROZEN_MAIN" ] || die "identity.json does not describe a line distinct from the frozen v0.3.2 line"
MSI_NAME="${PRODUCT}_${VERSION}_x64_en-US.msi"
NSIS_NAME="${PRODUCT}_${VERSION}_x64-setup.exe"
FROZEN_MSI="$(idj frozen.product_name)_$(idj frozen.version)_x64_en-US.msi"
FROZEN_NSIS="$(idj frozen.product_name)_$(idj frozen.version)_x64-setup.exe"

STAGE=""; MODE_DRY=0; MODE_THROW=0; MODE_REAL=0; AUTH_SHA=""; SCOPE_FILE=""; LEDGER=""; ALLOW_REUSE=0; DISTRIBUTION=0; LISTING=""
APPNAME="$PRODUCT"; APPURL=""; OUTDIR=""; THROWAWAY_DIR="$SELF_DIR/dryrun/throwaway"; TSA_URL=""
TOOLS_DIR="$SELF_DIR/tools"; MS_ROOT_PEM="$SELF_DIR/docs/pki/root2020.pem"
PY="$SELF_DIR/venv/v/bin/python"; VERIFY_PY=""
INPUTS=(); HASHES=()
TSA_PROD="http://timestamp.acs.microsoft.com"
JSIGN_SHA256="6cf58aa080e7ff82656f320cff8d4a3bff30408df8766806dcba3740dd839fa5"   # jsign-7.6.jar (GitHub release digest), as in v2
MS_ROOT_SHA1="f40042e2e5f7e8ef8189fed15519aece42c3bfa2"

while [ $# -gt 0 ]; do
  case "$1" in
    --stage)            [ $# -ge 2 ] || die "--stage needs a value"; STAGE="$2"; shift 2;;
    --input)            [ $# -ge 2 ] || die "--input needs a value"; INPUTS+=("$2"); shift 2;;
    --sha256)           [ $# -ge 2 ] || die "--sha256 needs a value"; HASHES+=("$(echo "$2" | tr 'A-F' 'a-f')"); shift 2;;
    --outdir)           [ $# -ge 2 ] || die "--outdir needs a value"; OUTDIR="$2"; shift 2;;
    --name)             [ $# -ge 2 ] || die "--name needs a value"; APPNAME="$2"; shift 2;;
    --url)              [ $# -ge 2 ] || die "--url needs a value"; APPURL="$2"; shift 2;;
    --throwaway-dir)    [ $# -ge 2 ] || die "--throwaway-dir needs a value"; THROWAWAY_DIR="$2"; shift 2;;
    --tsa-url)          [ $# -ge 2 ] || die "--tsa-url needs a value"; TSA_URL="$2"; shift 2;;
    --tools-dir)        [ $# -ge 2 ] || die "--tools-dir needs a value"; TOOLS_DIR="$2"; shift 2;;
    --ms-root-pem)      [ $# -ge 2 ] || die "--ms-root-pem needs a value"; MS_ROOT_PEM="$2"; shift 2;;
    --python)           [ $# -ge 2 ] || die "--python needs a value"; PY="$2"; shift 2;;
    --verifier)         [ $# -ge 2 ] || die "--verifier needs a value"; VERIFY_PY="$2"; shift 2;;
    --i-have-owner-authorization) [ $# -ge 2 ] || die "--i-have-owner-authorization needs the sha256 of the scope file"; AUTH_SHA="$2"; shift 2;;
    --scope-file)       [ $# -ge 2 ] || die "--scope-file needs a value"; SCOPE_FILE="$2"; shift 2;;
    --ledger)           [ $# -ge 2 ] || die "--ledger needs a value"; LEDGER="$2"; shift 2;;
    --container-listing) [ $# -ge 2 ] || die "--container-listing needs a value"; LISTING="$2"; shift 2;;
    --allow-reuse)      ALLOW_REUSE=1; shift;;
    --distribution)     DISTRIBUTION=1; shift;;
    --dry-run)          MODE_DRY=1; shift;;
    --throwaway-test)   MODE_THROW=1; shift;;
    --real)             MODE_REAL=1; shift;;
    -h|--help)          usage; exit 0;;
    *) usage >&2; die "unknown argument: $1";;
  esac
done
[ $((MODE_DRY + MODE_THROW + MODE_REAL)) -le 1 ] || die "give at most one of --dry-run, --throwaway-test, --real"
if [ $((MODE_DRY + MODE_THROW + MODE_REAL)) -eq 0 ]; then MODE_DRY=1; fi
if [ "$MODE_REAL" = 1 ]; then MODE=real; elif [ "$MODE_THROW" = 1 ]; then MODE=throwaway-test; else MODE=dry-run; fi
PLAN=0; [ "$MODE" = dry-run ] && PLAN=1
[ "$(uname -s)" = Darwin ] || die "macOS only (the signing host)"
[ "$(id -u)" -ne 0 ] || die "do not run as root"

# ---- service coordinates: validated FIRST, in every mode, before any token could be requested (F3 of the frozen v2)
RE_ENDPOINT='^https://[a-z0-9]+\.codesigning\.azure\.net/?$'
RE_NAME='^[A-Za-z0-9-]{3,63}$'
RE_CN='^[A-Za-z0-9 ._&()-]{1,64}$'
has_ctl() { case "$1" in *[[:space:][:cntrl:]]*) return 0;; esac; return 1; }
ENDPOINT="${R0P_AZ_ENDPOINT:-}"; ACCOUNT="${R0P_AZ_ACCOUNT:-}"; PROFILE="${R0P_AZ_PROFILE:-}"; EXPECTED_SUBJECT="${R0P_AZ_EXPECTED_CN:-}"
EXPECTED_CN=""
validate_service_args() {
  if [ "$MODE" = real ]; then
    { [ -n "$ENDPOINT" ] && [ -n "$ACCOUNT" ] && [ -n "$PROFILE" ] && [ -n "$EXPECTED_SUBJECT" ]; } || die "--real needs R0P_AZ_ENDPOINT, R0P_AZ_ACCOUNT, R0P_AZ_PROFILE and R0P_AZ_EXPECTED_CN in the environment (supplied by the owner)"
  fi
  if [ -n "$ENDPOINT" ]; then
    if has_ctl "$ENDPOINT"; then die "R0P_AZ_ENDPOINT contains whitespace or control characters; refusing"; fi
    [[ "$ENDPOINT" =~ $RE_ENDPOINT ]] || die "R0P_AZ_ENDPOINT is not an allowed Artifact Signing endpoint (must match $RE_ENDPOINT); refusing before any token is requested"
  fi
  if [ -n "$ACCOUNT" ]; then
    if has_ctl "$ACCOUNT"; then die "R0P_AZ_ACCOUNT contains whitespace or control characters; refusing"; fi
    [[ "$ACCOUNT" =~ $RE_NAME ]] || die "R0P_AZ_ACCOUNT is not allowed (must match $RE_NAME)"
  fi
  if [ -n "$PROFILE" ]; then
    if has_ctl "$PROFILE"; then die "R0P_AZ_PROFILE contains whitespace or control characters; refusing"; fi
    [[ "$PROFILE" =~ $RE_NAME ]] || die "R0P_AZ_PROFILE is not allowed (must match $RE_NAME)"
  fi
  if [ -n "$EXPECTED_SUBJECT" ]; then
    case "$EXPECTED_SUBJECT" in CN=*) EXPECTED_CN="${EXPECTED_SUBJECT#CN=}";; *) die "R0P_AZ_EXPECTED_CN must have the form CN=<exact common name>";; esac
    [[ "$EXPECTED_CN" =~ $RE_CN ]] || die "the expected common name has characters outside $RE_CN (give only the CN value)"
  fi
}
validate_service_args
if [ "$MODE" != real ]; then
  [ -z "$AUTH_SHA$SCOPE_FILE" ] || die "--i-have-owner-authorization/--scope-file are used only with --real"
  [ -z "$LEDGER" ] && [ "$ALLOW_REUSE" = 0 ] && [ "$DISTRIBUTION" = 0 ] || die "--ledger, --allow-reuse and --distribution are used only with --real"
fi
# the Azure coordinates are in shell variables only now: no child process inherits them
unset R0P_AZ_ENDPOINT R0P_AZ_ACCOUNT R0P_AZ_PROFILE R0P_AZ_EXPECTED_CN
case "$STAGE" in inner|container) ;; *) die "--stage must be 'inner' or 'container'";; esac
[ ${#INPUTS[@]} -ge 1 ] || die "at least one --input is required"
[ ${#INPUTS[@]} -eq ${#HASHES[@]} ] || die "each --input needs a matching --sha256 (got ${#INPUTS[@]} inputs, ${#HASHES[@]} hashes)"
[ -n "$OUTDIR" ] || die "--outdir is required"
if [ -n "$TSA_URL" ] && [ "$MODE" != throwaway-test ]; then die "--tsa-url is for --throwaway-test only; production always uses $TSA_PROD"; fi
if [ -n "$TSA_URL" ]; then case "$TSA_URL" in http://127.0.0.1*|http://localhost*) ;; *) die "--tsa-url must be a local test TSA";; esac; fi
[[ "$APPNAME" =~ ^[A-Za-z0-9][A-Za-z0-9\ ._-]{0,63}$ ]] || die "--name has unexpected characters"
[ -z "$APPURL" ] || [[ "$APPURL" =~ ^https://[A-Za-z0-9._/-]{1,200}$ ]] || die "--url must be an https URL of letters, digits, '.', '_', '-' and '/'"

sha() { shasum -a 256 "$1" | awk '{print $1}'; }
SELF_SHA="$(sha "$SELF")"
if [ -z "$VERIFY_PY" ]; then
  if [ -f "$SELF_DIR/verify_signature_v2.py" ]; then VERIFY_PY="$SELF_DIR/verify_signature_v2.py"; else VERIFY_PY="$SELF_DIR/script/verify_signature_v2.py"; fi
fi
[ -f "$VERIFY_PY" ] || die "missing verify_signature_v2.py ($VERIFY_PY)"
VERIFY_SHA="$(sha "$VERIFY_PY")"

# ---- file-name policy per stage (frozen names are refused; the role decides what the file is)
role_of() { # basename -> prints role, or returns 1
  local b="$1"
  if [ "$b" = "$MSI_NAME" ]; then echo msi; return 0; fi
  if [ "$b" = "$NSIS_NAME" ]; then echo nsis; return 0; fi
  if [[ "$b" =~ ^(launcher|sidecar|uninstaller)-([0-9a-f]{64})\.exe$ ]]; then echo "${BASH_REMATCH[1]}"; return 0; fi
  return 1
}
i=0; ROLES=()
while [ $i -lt ${#INPUTS[@]} ]; do
  f="${INPUTS[$i]}"; want="${HASHES[$i]}"; b="$(basename "$f")"
  [[ "$want" =~ ^[0-9a-f]{64}$ ]] || die "--sha256 must be 64 hex chars"
  [ -f "$f" ] && [ ! -L "$f" ] || die "input not found or a symlink: $f"
  if [ "$b" = "$FROZEN_MSI" ] || [ "$b" = "$FROZEN_NSIS" ]; then die "$b is a FROZEN v0.3.2 artifact name; this script signs only R0-prime files"; fi
  role="$(role_of "$b")" || die "file name not allowed: $b (stage inner: <launcher|sidecar|uninstaller>-<sha256>.exe from r0prime_win_signcommand.sh; stage container: exactly $MSI_NAME or $NSIS_NAME)"
  case "$STAGE:$role" in
    inner:launcher|inner:sidecar|inner:uninstaller|container:msi|container:nsis) ;;
    *) die "role '$role' ($b) does not belong to stage '$STAGE': inner files are signed first, containers last";;
  esac
  got="$(sha "$f")"
  [ "$got" = "$want" ] || die "sha256 mismatch for $b: expected $want, got $got"
  case "$role" in launcher|sidecar|uninstaller)
    [[ "$b" == *"-$got.exe" ]] || die "$b: the sha256 in the file name is not the file's sha256 (the hand-off looks files up by it)";; esac
  j=0
  while [ $j -lt $i ]; do
    [ "$(basename "${INPUTS[$j]}")" != "$b" ] || die "two inputs have the same file name ($b); they would overwrite each other in --outdir"
    j=$((j+1))
  done
  ROLES+=("$role")
  i=$((i+1))
done

# ---- output directory rules (same as v2: new, or only a FAILED marker)
OUT_PARENT="$(dirname "$OUTDIR")"
[ -d "$OUT_PARENT" ] || die "the parent directory of --outdir does not exist: $OUT_PARENT (the script does not create parents)"
OUTDIR="$(cd "$OUT_PARENT" && pwd -P)/$(basename "$OUTDIR")"
failed_remnant() { # dir
  local d="$1" n
  [ -d "$d" ] && [ ! -L "$d" ] || return 1
  n="$(ls -A "$d" | wc -l | tr -d ' ')"
  [ "$n" = 1 ] && [ -f "$d/FAILED" ] && [ ! -L "$d/FAILED" ] && [ "$(head -1 "$d/FAILED")" = "$FAILED_MAGIC" ]
}
if [ -e "$OUTDIR" ] || [ -L "$OUTDIR" ]; then
  failed_remnant "$OUTDIR" || die "--outdir '$OUTDIR' already exists; choose a new directory (nothing is overwritten; only a directory holding just the FAILED marker of a failed attempt is reused)"
fi
for f in "${INPUTS[@]}"; do case "$(cd "$(dirname "$f")" && pwd -P)/" in "$OUTDIR"/*) die "--outdir must not contain an input";; esac; done

# ---- real mode: scope file
if [ "$MODE" = real ]; then
  [[ "$AUTH_SHA" =~ ^[0-9a-fA-F]{64}$ ]] || die "--real needs --i-have-owner-authorization <sha256 of the scope file>"
  AUTH_SHA="$(echo "$AUTH_SHA" | tr 'A-F' 'a-f')"
  [ -n "$SCOPE_FILE" ] || die "--real needs --scope-file FILE"
  # read ONCE into a private copy, hash the copy, parse the copy. The scope file is a TRIPWIRE against mistakes, NOT an authentication:
  # the owner's own terminal and `az` login are the authentication (round 4, R4-04).
  r0p_mktmp
  r0p_scope_load "$SCOPE_FILE" "$AUTH_SHA" scope_version not_before not_after script_sha256 common_sha256 verifier_sha256 identity_json_sha256 \
    stage inputs_sha256 az_endpoint az_account az_profile expected_cn gate_sha256 gate_tool_sha256 distribution evidence_manifest_sha256 \
    build_commit procedure_commit tool_az_sha256 tool_java_sha256 tool_python_sha256 tool_osslsigncode_sha256 container_listing_sha256
  [ "$S_scope_version" = 2 ] || die "scope file: scope_version must be 2 (v3.1 format; see AUTHORIZATION-TEMPLATE.md)"
  r0p_window_check "$S_not_before" "$S_not_after"
  [ "$S_script_sha256" = "$SELF_SHA" ] || die "scope file: script_sha256 does not match this script"
  [ "$S_common_sha256" = "$COMMON_SHA" ] || die "scope file: common_sha256 does not match signing-common.sh"
  [ "$S_verifier_sha256" = "$VERIFY_SHA" ] || die "scope file: verifier_sha256 does not match the verifier"
  [ "$S_identity_json_sha256" = "$(sha "$IDJSON")" ] || die "scope file: identity_json_sha256 does not match packaging/identity.json"
  [ "$S_stage" = "$STAGE" ] || die "scope file: stage '$S_stage' differs from --stage '$STAGE'"
  SORTED="$(printf '%s\n' "${HASHES[@]}" | sort | tr '\n' ',' | sed 's/,$//')"
  [ "$S_inputs_sha256" = "$SORTED" ] || die "scope file: inputs_sha256 does not list exactly the sha256 of the given inputs (sorted, comma separated)"
  { [ "$S_az_endpoint" = "$ENDPOINT" ] && [ "$S_az_account" = "$ACCOUNT" ] && [ "$S_az_profile" = "$PROFILE" ]; } || die "scope file: the service coordinates differ from R0P_AZ_ENDPOINT/ACCOUNT/PROFILE"
  [ "$S_expected_cn" = "$EXPECTED_SUBJECT" ] || die "scope file: expected_cn differs from R0P_AZ_EXPECTED_CN"
  [[ "$S_build_commit" =~ ^[0-9a-f]{40}$ ]] && [[ "$S_procedure_commit" =~ ^[0-9a-f]{40}$ ]] || die "scope file: build_commit and procedure_commit must be 40-hex git commit ids"
  [[ "$S_evidence_manifest_sha256" =~ ^[0-9a-f]{64}$ ]] || die "scope file: evidence_manifest_sha256 must be 64 hex chars"
  case "$S_distribution:$DISTRIBUTION" in yes:1|no:0) ;; *) die "scope file says distribution=$S_distribution but --distribution is $( [ "$DISTRIBUTION" = 1 ] && echo given || echo not given ): they must agree";; esac
  if [ "$STAGE" = container ]; then
    [ -n "$LISTING" ] || die "stage container in --real needs --container-listing FILE (the file list of the rebuilt installers, made on the Windows build host)"
    [[ "$S_container_listing_sha256" =~ ^[0-9a-f]{64}$ ]] || die "scope file: container_listing_sha256 must be 64 hex chars for stage container"
  else
    [ -z "$LISTING" ] && [ "$S_container_listing_sha256" = none ] || die "scope file: container_listing_sha256 must be 'none' and no --container-listing given for stage inner"
  fi
  r0p_ledger_check "$LEDGER" "$AUTH_SHA" "$ALLOW_REUSE"
  r0p_gate windows "$DISTRIBUTION" "$S_gate_sha256" "$S_gate_tool_sha256"
fi
# the container listing (what the installers hold, from the build host) is read once into a private copy and recorded in the evidence
LISTING_SHA="none"; LISTING_LINES=0
if [ -n "$LISTING" ]; then
  [ "$STAGE" = container ] || die "--container-listing belongs to stage container"
  [ -f "$LISTING" ] && [ ! -L "$LISTING" ] || die "--container-listing must be a regular file"
  [ -n "$R0P_TMP" ] || r0p_mktmp
  cat "$LISTING" > "$R0P_TMP/listing.copy"
  LISTING_SHA="$(sha "$R0P_TMP/listing.copy")"; LISTING_LINES="$(wc -l < "$R0P_TMP/listing.copy" | tr -d ' ')"
  [ "$LISTING_LINES" -ge 1 ] || die "--container-listing is empty"
  if [ "$MODE" = real ]; then [ "$LISTING_SHA" = "$S_container_listing_sha256" ] || die "scope file: container_listing_sha256 does not match --container-listing"; fi
fi

# ---- tools (not needed to print a plan)
JAVA=""; JSIGN="$TOOLS_DIR/jsign/jsign-7.6.jar"; OSSL="$TOOLS_DIR/ossbuild/osslsigncode"; [ -x "$OSSL" ] || OSSL=""
if [ "$PLAN" = 0 ]; then
  JAVA_HOME_DIR="$(ls -d "$TOOLS_DIR"/jdk/x/*/Contents/Home 2>/dev/null | head -1 || true)"
  if [ -n "${JAVA_HOME_DIR:-}" ] && [ -x "$JAVA_HOME_DIR/bin/java" ]; then JAVA="$JAVA_HOME_DIR/bin/java"; else JAVA="$(command -v java || true)"; fi
  [ -n "${JAVA:-}" ] && "$JAVA" -version >/dev/null 2>&1 || die "no usable Java found (expected portable JDK under <tools-dir>/jdk)"
  [ -f "$JSIGN" ] || die "missing <tools-dir>/jsign/jsign-7.6.jar"
  [ "$(sha "$JSIGN")" = "$JSIGN_SHA256" ] || die "jsign jar sha256 mismatch (pinned $JSIGN_SHA256)"
  [ -x "$PY" ] || die "missing python venv with signify ($PY)"
  AZ_PATH=""
  if [ "$MODE" = real ]; then
    AZ_PATH="$(command -v az 2>/dev/null || true)"; [ -n "$AZ_PATH" ] || die "Azure CLI (az) not found in the fixed PATH or R0P_TOOLS_DIR"
    # every external non-system tool is pinned by the sha256 of the file the fixed PATH resolves to (round 4, R4-05)
    JAVA_ABS="$(cd "$(dirname "$JAVA")" && pwd -P)/$(basename "$JAVA")"
    PY_ABS="$(cd "$(dirname "$PY")" && pwd -P)/$(basename "$PY")"
    [ "$(sha "$AZ_PATH")" = "$S_tool_az_sha256" ] || die "scope file: tool_az_sha256 does not match $(basename "$AZ_PATH") ($(sha "$AZ_PATH"))"
    [ "$(sha "$JAVA_ABS")" = "$S_tool_java_sha256" ] || die "scope file: tool_java_sha256 does not match the java executable ($(sha "$JAVA_ABS"))"
    [ "$(sha "$PY_ABS")" = "$S_tool_python_sha256" ] || die "scope file: tool_python_sha256 does not match the python executable ($(sha "$PY_ABS"))"
    if [ -n "$OSSL" ]; then [ "$(sha "$OSSL")" = "$S_tool_osslsigncode_sha256" ] || die "scope file: tool_osslsigncode_sha256 does not match osslsigncode"
    else [ "$S_tool_osslsigncode_sha256" = none ] || die "scope file: tool_osslsigncode_sha256 is not 'none' but osslsigncode is absent"; fi
  fi
  # inputs must be unsigned (a second signature is never added); an unreadable file fails closed
  i=0
  while [ $i -lt ${#INPUTS[@]} ]; do
    f="${INPUTS[$i]}"
    nsig="$("$PY" -I - "$f" <<'PYEOF'
import sys
from signify.authenticode.signed_file import SignedPEFile
try:
    from signify.authenticode.signed_file import SignedMsiFile
except ImportError:
    SignedMsiFile = None
fn = sys.argv[1]
cls = SignedMsiFile if fn.lower().endswith(".msi") else SignedPEFile
with open(fn, "rb") as fh:
    try:
        print(len(list(cls(fh).iter_embedded_signatures())))
    except Exception as e:
        # an unsigned MSI raises AuthenticodeNotSignedError: that means 0 signatures. Any OTHER error means the file
        # could not be inspected: report it, the caller refuses (fail closed).
        print(0 if type(e).__name__ == "AuthenticodeNotSignedError" else "ERR:" + type(e).__name__)
PYEOF
)"
    case "$nsig" in
      0) ;;
      ERR:*) die "$(basename "$f") could not be inspected for existing signatures ($nsig); refusing";;
      *) die "$(basename "$f") already carries $nsig signature(s); refusing to add another (use a fresh unsigned input)";;
    esac
    i=$((i+1))
  done
fi
CLEAN_ENV=(env -u SSH_AUTH_SOCK -u SSH_AGENT_PID -u APPROVAL_SECRET -u AZURE_CLIENT_SECRET)

# ---- plan (dry-run): print and stop; nothing is created
JSIGN_NAME_ARGS=(--name "$APPNAME"); [ -n "$APPURL" ] && JSIGN_NAME_ARGS+=(--url "$APPURL")
TIMEOUT_S="${R0P_SIGN_TIMEOUT_SECONDS:-900}"
[[ "$TIMEOUT_S" =~ ^[0-9]{1,5}$ ]] || die "R0P_SIGN_TIMEOUT_SECONDS must be a number of seconds"
log "$SCRIPT_VERSION  mode: $MODE  stage: $STAGE  files: ${#INPUTS[@]}"
log "line: product=$PRODUCT version=$VERSION; containers: $MSI_NAME / $NSIS_NAME; launcher=$MAIN_NAME.exe sidecar=$SIDECAR_NAME.exe"
log "script sha256=$SELF_SHA verifier sha256=$VERIFY_SHA"
if [ "$MODE" = real ]; then log "timestamp authority: $TSA_PROD (RFC 3161); Azure sign-API calls this run: expected $(( ${#INPUTS[@]} + 1 )) (1 chain-fetch + 1 per file; from Jsign's source, not observed)"; fi
if [ "$PLAN" = 1 ]; then
  i=0; while [ $i -lt ${#INPUTS[@]} ]; do log "input: $(basename "${INPUTS[$i]}") role=${ROLES[$i]} sha256=${HASHES[$i]} (verified)"; i=$((i+1)); done
  log "DRY-RUN: the commands below are the --real sequence, printed and NOT executed; nothing was created, no token was requested."
  echo "+ az account get-access-token --resource https://codesigning.azure.net --query accessToken -o tsv > <outdir>/work/tok/raw   # token into a 0600 file; never printed"
  echo "+ java -Djava.io.tmpdir=<outdir>/work/tmp -jar <tools-dir>/jsign/jsign-7.6.jar --storetype TRUSTEDSIGNING --keystore <R0P_AZ_ENDPOINT> --storepass file:<outdir>/work/tok/token --alias <R0P_AZ_ACCOUNT>/<R0P_AZ_PROFILE> --alg SHA-256 --tsmode RFC3161 --tsaurl $TSA_PROD --tsretries 3 --tsretrywait 10 $(qcmd "${JSIGN_NAME_ARGS[@]}") <files in <outdir>/work/signing>"
  echo "+ <python> -I $(basename "$VERIFY_PY") --require-timestamp --expect-cn <R0P_AZ_EXPECTED_CN value> --expect-eku 1.3.6.1.5.5.7.3.3 --expect-eku 1.3.6.1.4.1.311.97.1.0 <files>"
  echo "+ <tools-dir>/ossbuild/osslsigncode verify -CAfile <ms-root-pem> -TSA-CAfile <ms-root-pem> -ignore-crl -in <file>   # informational"
  echo "+ mv <outdir>/work/signing/<file> <outdir>/<file>   # only after verification; then EVIDENCE.txt and SHA256SUMS"
  if [ "$STAGE" = inner ]; then echo "NEXT (inner stage): hand the signed files back to the Windows build host (r0prime_win_signcommand.sh apply), rebuild the containers, then run --stage container."; fi
  exit 0
fi
if [ "$MODE" = throwaway-test ]; then
  { [ -f "$THROWAWAY_DIR/throwaway.p12" ] && [ -f "$THROWAWAY_DIR/ks.pass" ] && [ -f "$THROWAWAY_DIR/ca.crt" ]; } \
    || die "throwaway keystore not found in <throwaway-dir> (create with script/make_throwaway_cert.sh of the frozen set)"
fi

# ---- state, cleanup, signals (as v2)
OUTDIR_CREATED=0; SUCCESS=0; JPID=""; LAST_LINE=""; LEDGER_STARTED=0
WORK=""; TOKDIR=""; SIGNDIR=""; WTMP=""
trap 'LAST_LINE=$LINENO' ERR
wipe_tree() { # dir: truncate+remove files one by one (named rm -f), then rmdir deepest-first. Never rm -rf.
  local d="$1" p
  [ -d "$d" ] && [ ! -L "$d" ] || return 0
  find "$d" -depth \( -type f -o -type l \) -print0 | while IFS= read -r -d '' p; do [ -L "$p" ] || : > "$p" 2>/dev/null; rm -f "$p"; done
  find "$d" -depth -type d -print0 | while IFS= read -r -d '' p; do rmdir "$p" 2>/dev/null || true; done
}

# F7: scrub local paths / user name from text that goes into EVIDENCE.txt
scrub() {
  local line u; u="$(id -un 2>/dev/null || true)"
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "${SIGNDIR:-}" ] && line="${line//"$SIGNDIR"/<outdir>/work/signing}"
    [ -n "${OUTDIR:-}" ] && line="${line//"$OUTDIR"/<outdir>}"
    line="${line//"$THROWAWAY_DIR"/<throwaway-dir>}"
    line="${line//"$MS_ROOT_PEM"/<ms-root-pem>}"
    line="${line//"$TOOLS_DIR"/<tools-dir>}"
    line="${line//"$SELF_DIR"/<script-dir>}"
    if [ -n "${HOME:-}" ] && [ "$HOME" != "/" ]; then line="${line//"$HOME"/~}"; fi
    if [ "${#u}" -ge 4 ]; then line="${line//"$u"/<user>}"; fi
    case "$line" in
      */Users/*|*/private/*|*/home/*|*/var/folders/*|*/tmp/*|*/Volumes/*) line="[redacted: line contained a local path]";;
    esac
    printf '%s\n' "$line"
  done
}

on_exit() {
  local rc=$? detail="" p reason
  trap - EXIT; trap '' INT TERM HUP
  set +e +x
  if [ -n "${TOKDIR:-}" ]; then wipe_tree "$TOKDIR"; fi
  if [ "${LEDGER_STARTED:-0}" = 1 ]; then r0p_ledger_append "$LEDGER" "$AUTH_SHA" real ended "rc=$rc success=$SUCCESS"; fi
  r0p_tmp_cleanup
  if [ "$OUTDIR_CREATED" = 1 ]; then
    if [ "$rc" -ne 0 ] || [ "$SUCCESS" != 1 ]; then
      [ "$rc" -ne 0 ] || rc=1
      reason="${FAIL_REASON:-command failed near line ${LAST_LINE:-?}}"
      if [ -f "$WORK/verify.out" ]; then detail="$(head -c 20000 "$WORK/verify.out" | scrub)"; fi
      wipe_tree "$WORK"
      for p in "${NAMES[@]:-}"; do [ -n "$p" ] && rm -f "${OUTDIR:?}/${p:?}"; done
      rm -f "${OUTDIR:?}/EVIDENCE.txt" "${OUTDIR:?}/SHA256SUMS" "${OUTDIR:?}/EVIDENCE.txt.tmp" "${OUTDIR:?}/SHA256SUMS.tmp" "${OUTDIR:?}/FAILED"
      {
        echo "$FAILED_MAGIC"
        echo "failed (exit status $rc) at $(date -u +%Y-%m-%dT%H:%M:%SZ) by $SELF_NAME version $SCRIPT_VERSION (mode $MODE, stage $STAGE)"
        echo "reason: $reason"
        echo "No signed, unsigned or partially signed copy of any input was left in this directory."
        echo "This directory contains only this marker; running the script again with the same --outdir is accepted."
        echo "If a signature was already requested from the service, a new run asks the service again (quota): ask the owner before retrying."
        if [ -n "$detail" ]; then echo; echo "verifier output:"; echo "$detail"; fi
      } > "$OUTDIR/FAILED"
      echo "FAILED: $reason (details: <outdir>/FAILED)" >&2
    else
      wipe_tree "$WORK"
      [ ! -d "$WORK" ] || echo "WARNING: work directory could not be fully removed" >&2
    fi
  fi
  exit "$rc"
}
on_signal() { # name exit-status
  trap '' INT TERM HUP
  FAIL_REASON="interrupted by SIG$1"
  if [ -n "$JPID" ]; then kill "$JPID" 2>/dev/null || true; wait "$JPID" 2>/dev/null || true; JPID=""; fi
  exit "$2"
}
trap on_exit EXIT
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM
trap 'on_signal HUP 129' HUP

# ---- create the output directory and the work area
if [ -e "$OUTDIR" ]; then rm -f "${OUTDIR:?}/FAILED"; rmdir "$OUTDIR" || die "could not clear the previous failed-attempt directory"; fi
mkdir "$OUTDIR"; OUTDIR_CREATED=1; chmod 700 "$OUTDIR"
WORK="$OUTDIR/work"; TOKDIR="$WORK/tok"; SIGNDIR="$WORK/signing"; WTMP="$WORK/tmp"
mkdir "$WORK" "$TOKDIR" "$SIGNDIR" "$WTMP"; chmod 700 "$WORK" "$TOKDIR" "$SIGNDIR" "$WTMP"
export TMPDIR="$WTMP"
NAMES=(); OUTS=()
i=0
while [ $i -lt ${#INPUTS[@]} ]; do
  b="$(basename "${INPUTS[$i]}")"
  NAMES+=("$b")
  cp "${INPUTS[$i]}" "$SIGNDIR/$b"; chmod 600 "$SIGNDIR/$b"
  # TOCTOU (round 4, R4-02): what was copied must be what was hashed and authorized; sign only the copy
  [ "$(sha "$SIGNDIR/$b")" = "${HASHES[$i]}" ] || die "$b changed between the hash check and the copy (the copy is not the authorized file); nothing was signed"
  OUTS+=("$SIGNDIR/$b"); i=$((i+1))
done
# immediately before every signing call: the files in the private directory still are the authorized bytes
verify_copies() {
  local k
  k=0
  while [ $k -lt ${#NAMES[@]} ]; do
    [ "$(sha "$SIGNDIR/${NAMES[$k]}")" = "${HASHES[$k]}" ] || die "${NAMES[$k]} in the work directory is not the authorized file (hash differs); refusing to sign"
    k=$((k+1))
  done
}

# ---- sign
JAVA_TMP_ARG="-Djava.io.tmpdir=$WTMP"
# run a command in the background with a watchdog and wait for it, so INT/TERM/HUP are handled at once and a hung signer is ended
run_child() {
  "$@" &
  JPID=$!
  ( sleep "$TIMEOUT_S"; kill "$JPID" 2>/dev/null ) >/dev/null 2>&1 &
  local wd=$! rc=0
  wait "$JPID" || rc=$?
  kill "$wd" 2>/dev/null || true; wait "$wd" 2>/dev/null || true
  JPID=""
  return "$rc"
}

if [ "$MODE" = throwaway-test ]; then
  TS_ARGS=()
  ANCHORS="$THROWAWAY_DIR/ca.crt"
  if [ -n "$TSA_URL" ]; then
    TS_ARGS=(--tsmode RFC3161 --tsaurl "$TSA_URL" --tsretries 1)
    if [ -f "$THROWAWAY_DIR/../tsa/anchors.pem" ]; then ANCHORS="$THROWAWAY_DIR/../tsa/anchors.pem"; fi
  fi
  log "signing with THROWAWAY keystore (no network except optional local TSA)"
  verify_copies
  run_child "${CLEAN_ENV[@]}" "$JAVA" "$JAVA_TMP_ARG" -jar "$JSIGN" --keystore "$THROWAWAY_DIR/throwaway.p12" --storetype PKCS12 \
      --storepass "file:$THROWAWAY_DIR/ks.pass" --alias throwaway-leaf --alg SHA-256 \
      "${JSIGN_NAME_ARGS[@]}" ${TS_ARGS[@]+"${TS_ARGS[@]}"} "${OUTS[@]}" || die "Jsign failed (exit status $?)"
else
  validate_service_args      # again, immediately before the only token request (defence in depth)
  # the scope is consumed from here on (the first use of the owner's az session): a failed run burns it
  r0p_ledger_append "$LEDGER" "$AUTH_SHA" real started "script=$SELF_SHA stage=$STAGE inputs=$SORTED"
  LEDGER_STARTED=1
  TOKFILE="$TOKDIR/token"; RAW="$TOKDIR/raw"
  log "service: $ENDPOINT account=$ACCOUNT profile=$PROFILE"
  log "obtaining access token via az (written to a 0600 file; never printed)"
  # The ONLY place a token is requested. Output goes straight to the file, not to the terminal/logs.
  ( set +x; "${CLEAN_ENV[@]}" az account get-access-token --resource https://codesigning.azure.net \
      --query accessToken -o tsv > "$RAW" ) || die "az could not obtain a token (is 'az login' valid? is the signer role assigned?)"
  tr -d '\r\n' < "$RAW" > "$TOKFILE"; : > "$RAW"; rm -f "$RAW"; chmod 600 "$TOKFILE"
  [ -s "$TOKFILE" ] || die "empty token"
  log "signing ${#OUTS[@]} file(s) in ONE Jsign invocation (the secure timestamp is mandatory)"
  verify_copies
  run_child "${CLEAN_ENV[@]}" "$JAVA" "$JAVA_TMP_ARG" -jar "$JSIGN" --storetype TRUSTEDSIGNING --keystore "$ENDPOINT" \
      --storepass "file:$TOKFILE" --alias "$ACCOUNT/$PROFILE" --alg SHA-256 \
      --tsmode RFC3161 --tsaurl "$TSA_PROD" --tsretries 3 --tsretrywait 10 \
      "${JSIGN_NAME_ARGS[@]}" "${OUTS[@]}" || die "Jsign failed or timed out after ${TIMEOUT_S}s (exit status $?)"
  wipe_tree "$TOKDIR"; mkdir -p "$TOKDIR"; chmod 700 "$TOKDIR"
  log "token file removed"
fi

# ---- verify, still inside work/signing (nothing is in --outdir yet)
if [ "$MODE" = throwaway-test ]; then
  VARGS=(--trust-anchor "$ANCHORS" --expect-eku 1.3.6.1.5.5.7.3.3)
  if [ -n "$TSA_URL" ]; then VARGS+=(--require-timestamp); fi
else
  VARGS=(--require-timestamp --expect-cn "$EXPECTED_CN" --expect-eku 1.3.6.1.5.5.7.3.3 --expect-eku 1.3.6.1.4.1.311.97.1.0)
fi
VERIFY_RC=0
( cd "$SIGNDIR" && "$PY" -I "$VERIFY_PY" ${VARGS[@]+"${VARGS[@]}"} "${NAMES[@]}" ) > "$WORK/verify.out" 2>&1 || VERIFY_RC=$?
if [ "$VERIFY_RC" -ne 0 ]; then
  cat "$WORK/verify.out" | scrub >&2
  FAIL_REASON="signify verification FAILED (exit status $VERIFY_RC); no signed file was released"
  exit 4
fi
log "signify verification PASS for ${#NAMES[@]} file(s)"
# the signed copies must differ from the inputs (a "Jsign" that signs nothing would otherwise pass an expected-hash comparison)
i=0
while [ $i -lt ${#NAMES[@]} ]; do
  if [ "$(sha "$SIGNDIR/${NAMES[$i]}")" = "${HASHES[$i]}" ]; then FAIL_REASON="the file ${NAMES[$i]} is byte-identical to its input after signing"; exit 4; fi
  i=$((i+1))
done

# ---- osslsigncode: independent, INFORMATIONAL. Its exit code is NOT used: osslsigncode 2.14 can print
# 'Timestamp Server Signature verification: failed' and still say 'Signature verification: ok / Succeeded' with exit 0.
# The script therefore reads the two verification lines itself and prints a WARNING if the timestamp line is not 'ok'.
OSSL_SUMMARY=(); OSSL_WARN=0
OSSL_TEXT="$WORK/ossl.out"; OSSL_RAW="$WORK/ossl.raw"; : > "$OSSL_RAW"
{
  echo "OSSLSIGNCODE VERIFICATION (independent implementation; INFORMATIONAL: the pass/fail decision is signify's above)"
  echo "  The osslsigncode exit code is NOT used. Per file the script reads the lines 'Signature verification:' and"
  echo "  'Timestamp Server Signature verification:' and records them below; a timestamp line that is not 'ok' is a WARNING."
  if [ -n "$OSSL" ]; then
    for b in "${NAMES[@]}"; do
      echo "  --- $b"
      OUT1="$WORK/ossl.$$.one"
      if [ "$MODE" = throwaway-test ]; then
        TSAARG=()
        if [ -n "$TSA_URL" ]; then
          # the TSA's trust anchor (anchors.pem = the throwaway root), else the TSA certificate itself
          if [ -f "$THROWAWAY_DIR/../tsa/anchors.pem" ]; then TSAARG=(-TSA-CAfile "$THROWAWAY_DIR/../tsa/anchors.pem")
          elif [ -f "$THROWAWAY_DIR/../tsa/tsa.crt" ]; then TSAARG=(-TSA-CAfile "$THROWAWAY_DIR/../tsa/tsa.crt"); fi
        fi
        if [ ${#TSAARG[@]} -gt 0 ]; then
          ( cd "$SIGNDIR" && "$OSSL" verify -CAfile "$THROWAWAY_DIR/ca.crt" "${TSAARG[@]}" -ignore-crl -in "$b" ) > "$OUT1" 2>&1 || true
        else
          ( cd "$SIGNDIR" && "$OSSL" verify -CAfile "$THROWAWAY_DIR/ca.crt" -ignore-timestamp -ignore-crl -in "$b" ) > "$OUT1" 2>&1 || true
        fi
        TSREQ=0; [ -n "$TSA_URL" ] && TSREQ=1
      elif [ -f "$MS_ROOT_PEM" ] && [ "$("$PY" -I -c 'import sys,hashlib,ssl;print(hashlib.sha1(ssl.PEM_cert_to_DER_cert(open(sys.argv[1]).read())).hexdigest())' "$MS_ROOT_PEM" 2>/dev/null)" = "$MS_ROOT_SHA1" ]; then
        ( cd "$SIGNDIR" && "$OSSL" verify -CAfile "$MS_ROOT_PEM" -TSA-CAfile "$MS_ROOT_PEM" -ignore-crl -in "$b" ) > "$OUT1" 2>&1 || true
        TSREQ=1
      else
        echo "    skipped: --ms-root-pem missing or SHA-1 != $MS_ROOT_SHA1"
        OSSL_SUMMARY+=("$b: skipped"); rm -f "$OUT1"; continue
      fi
      sed 's/^/    /' "$OUT1"
      SIGL="$(grep -m1 '^Signature verification:' "$OUT1" | sed 's/^Signature verification: *//' || true)"
      TSL="$(grep '^Timestamp Server Signature verification:' "$OUT1" | sed 's/^Timestamp Server Signature verification: *//' | tr '\n' ',' | sed 's/,$//' || true)"
      [ -n "$SIGL" ] || SIGL="(line absent)"
      if [ -z "$TSL" ]; then TSL="(line absent)"; fi
      TSNOTE=""
      if [ "$TSL" = "(line absent)" ]; then
        if [ "$TSREQ" -eq 1 ]; then TSNOTE=" -- WARNING: no timestamp verification line although a timestamp was expected"; fi
      elif [ -n "$(printf '%s' "$TSL" | tr -d 'ok,')" ]; then
        TSNOTE=" -- WARNING: timestamp line is not 'ok'"
      fi
      if [ -n "$TSNOTE" ]; then OSSL_WARN=1; fi
      OSSL_SUMMARY+=("$b: signature=$SIGL; timestamp=$TSL$TSNOTE")
      rm -f "$OUT1"
    done
    echo "  SUMMARY (parsed lines, exit codes ignored):"
    for s in "${OSSL_SUMMARY[@]:-}"; do [ -n "$s" ] && echo "    $s"; done
    if [ "$OSSL_WARN" -eq 1 ]; then
      echo "  *** WARNING: osslsigncode did not report 'Timestamp Server Signature verification: ok' for every file. osslsigncode"
      echo "  *** can still print 'Signature verification: ok' in that case (it falls back to the current time). The"
      echo "  *** timestamp is judged by signify above; investigate before release. ***"
    fi
  else
    echo "  osslsigncode not available"
  fi
} > "$OSSL_RAW" 2>&1
scrub < "$OSSL_RAW" > "$OSSL_TEXT"
if [ "$OSSL_WARN" -eq 1 ]; then echo "WARNING: osslsigncode did not confirm the timestamp for every file (informational check); see EVIDENCE" >&2; fi

# ---- evidence ----------------------------------------------------------------------------------------------------
EVID_TMP="$OUTDIR/EVIDENCE.txt.tmp"
{
  echo "EVIDENCE for Authenticode signing of Windows files, Agentic Apps R0-prime $VERSION"
  echo "========================================================="
  echo "script:          $SELF_NAME (script version $SCRIPT_VERSION) sha256 $SELF_SHA"
  echo "verifier:        $(basename "$VERIFY_PY") sha256 $VERIFY_SHA"
  echo "mode:            $MODE$( [ "$MODE" = throwaway-test ] && echo ' (throwaway key; NOT trusted by Windows)')"
  echo "stage:           $STAGE ($( [ "$STAGE" = inner ] && echo 'inner executables, signed BEFORE the installers are rebuilt' || echo 'installer containers, signed LAST'))"
  echo "generated (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host:            $(uname -srm)"
  echo "operator:        (not recorded: no OS user name in the evidence)"
  if [ "$MODE" = real ]; then
    echo "authorization:   scope file sha256 $AUTH_SHA (window $S_not_before .. $S_not_after UTC); TRIPWIRE against mistakes, not an authentication"
    echo "scope binds:     identity.json $S_identity_json_sha256; common $S_common_sha256; gate $S_gate_sha256; gate tool $S_gate_tool_sha256"
    echo "scope binds:     build commit $S_build_commit; procedure commit $S_procedure_commit (recorded, not verified); evidence manifest $S_evidence_manifest_sha256"
    echo "tools (sha256):  az $S_tool_az_sha256; java $S_tool_java_sha256; python $S_tool_python_sha256; osslsigncode $S_tool_osslsigncode_sha256"
    echo "ledger:          $LEDGER (--allow-reuse=$ALLOW_REUSE)"
    if [ "$DISTRIBUTION" = 1 ]; then echo "distribution:    gate enforced (--distribution) and passed"; else echo "distribution:    PRIVATE-CANDIDATE signing: signing does NOT imply permission to distribute"; fi
    echo "service:         $ENDPOINT account=$ACCOUNT profile=$PROFILE"
    echo "timestamp auth.: $TSA_PROD (RFC3161), mandatory"
    echo "subject check:   signer certificate must carry exactly one CN, equal to '$EXPECTED_CN' (exact match)"
    echo "azure calls:     expected $(( ${#INPUTS[@]} + 1 )) sign-API calls per Jsign's source; NOT observed or counted by this script"
    echo "production path: this script's real path had not been run against Azure when it was written (see the script header)"
  else
    if [ -n "$TSA_URL" ]; then TSTXT="local test TSA $TSA_URL"; else TSTXT="none"; fi
    echo "service:         NONE (throwaway key <throwaway-dir>/throwaway.p12); timestamp: $TSTXT"
  fi
  echo
  echo "TOOL VERSIONS"
  echo "  jsign:         7.6 (<tools-dir>/jsign/jsign-7.6.jar sha256 $JSIGN_SHA256)"
  echo "  java:          $("$JAVA" -version 2>&1 | head -1)"
  echo "  signify:       $("$PY" -I -c 'import importlib.metadata as m;print(m.version("signify"))')"
  if [ -n "$OSSL" ]; then echo "  osslsigncode:  $("$OSSL" --version 2>&1 | head -1)"; fi
  echo
  echo "FILES (role, hashes before / after)"
  i=0
  while [ $i -lt ${#INPUTS[@]} ]; do
    b="${NAMES[$i]}"
    echo "  $b   role=${ROLES[$i]}"
    echo "    before: sha256 ${HASHES[$i]}  size $(wc -c < "${INPUTS[$i]}" | tr -d ' ')"
    echo "    after:  sha256 $(shasum -a 256 "$SIGNDIR/$b" | awk '{print $1}')  size $(wc -c < "$SIGNDIR/$b" | tr -d ' ')"
    i=$((i+1))
  done
  echo
  echo "SIGNIFY VERIFICATION (digest match, PKCS#7 signature, chain to trust anchor, EKU, exact CN, timestamp; offline)"
  sed 's/^/  /' "$WORK/verify.out" | scrub
  echo
  cat "$OSSL_TEXT"
  echo
  if [ "$STAGE" = container ]; then echo "CONTAINER LISTING (file list of the installers from the build host): sha256 $LISTING_SHA, $LISTING_LINES lines; this script does NOT inspect the container contents"; echo; fi
  echo "LIMITS OF THIS EVIDENCE"
  echo "  * Verification here is offline and cross-platform (no CRL/OCSP). It shows the digest matches, the signature"
  echo "    is cryptographically valid, the chain reaches the pinned/known root and the timestamp countersignature is"
  echo "    present. It does NOT prove Windows accepts the file (Get-AuthenticodeSignature / signtool verify /pa,"
  echo "    SmartScreen, installer execution all require a Windows host) and says nothing about Windows platform support."
  if [ "$STAGE" = inner ]; then
    echo "  * Stage inner: the signed files are NOT yet in any installer. The containers must be rebuilt on the Windows build host from the"
    echo "    signed files and then signed (stage container). Signing inner files does not sign the containers."
  else
    echo "  * Stage container: whether the files INSIDE the containers carry the inner signatures is NOT checked by this script (it needs a"
    echo "    Windows host or an extraction tool); the Windows acceptance kit must check the installed files."
  fi
  echo "  * MsiDigitalSignatureEx: Jsign 7.6 never writes that stream into an MSI (osslsigncode warns); the frozen v0.3.2 MSI worked without it on"
  echo "    Windows Server 2022 (observed once); Windows 10/11 and interactive installs are not tested."
  echo "  * The signing certificates are valid for 3 days; every signature depends on the RFC 3161 timestamp to stay valid afterwards."
  if [ "$MODE" = throwaway-test ]; then echo "  * THROWAWAY-TEST: signed with a throwaway key; the signatures are NOT trusted by Windows."; fi
  echo
  echo "RESULT: signify verification PASS"
} 2>&1 | scrub > "$EVID_TMP"

# ---- release: move the verified files into --outdir (rename inside one file system), then checksums ----------------
i=0
while [ $i -lt ${#NAMES[@]} ]; do
  b="${NAMES[$i]}"
  pre="$(shasum -a 256 "$SIGNDIR/$b" | awk '{print $1}')"
  mv "$SIGNDIR/$b" "$OUTDIR/$b"
  post="$(shasum -a 256 "$OUTDIR/$b" | awk '{print $1}')"
  [ "$pre" = "$post" ] || die "file changed while being moved into --outdir: $b"
  i=$((i+1))
done
mv "$EVID_TMP" "$OUTDIR/EVIDENCE.txt"
( cd "$OUTDIR" && for b in "${NAMES[@]}"; do shasum -a 256 "$b"; done > SHA256SUMS.tmp && mv SHA256SUMS.tmp SHA256SUMS )
SUCCESS=1
log "evidence: <outdir>/EVIDENCE.txt"
log "done ($MODE, stage $STAGE)"
