#!/bin/bash -p
# sign_manifest_sshsig.sh  (script version r0prime-sshsig-2)
#
# Sign (or only verify) the top-level evidence manifest of an R0-prime evidence set with an OpenSSH signature (SSHSIG), namespace
# redevops-qa-release@v1, WITHOUT adding any file to the evidence directory: the manifest is streamed to `ssh-keygen -Y sign` on stdin and
# the signature is read from its stdout into a file OUTSIDE the evidence directory.
# Procedure and caveats: packaging/signing/SSHSIG-manifest-signing.md.
#
# THE OWNER'S KEY IS USED ONLY THROUGH THE OWNER'S ssh-agent (round 4, R4-01). In --real mode the script
#   * copies the PUBLIC key into its private 0700 temp directory and gives THAT copy to `ssh-keygen -f`, so there is no private key file
#     beside it that OpenSSH could fall back to (OpenSSH would otherwise load <name> next to <name>.pub when the agent lacks the key);
#   * requires the key's fingerprint in `ssh-add -l` of the owner's agent before it signs, and refuses otherwise (no fallback, ever);
#   * runs ssh-keygen with a throwaway HOME, SSH_ASKPASS=/usr/bin/false, no DISPLAY and `-U` (use the agent): no ~/.ssh is read, no prompt;
#   * never opens a private key file: a file that contains a private key is refused as --pubkey.
#
# MODES (exactly one; default --dry-run)
#   --dry-run      DEFAULT. Checks the manifest sha256 against --expected-sha256, parses the public key, prints the exact commands.
#                  Does not contact an agent, creates nothing, signs nothing.
#   --verify-only  Verifies an existing signature (--sig FILE) of --manifest with --pubkey. Needs no secret.
#   --self-test    The script GENERATES a throwaway ed25519 key itself in a fresh private temp directory (comment signproc-TEST-KEY),
#                  signs and verifies with it, and deletes it by name. There is no way to hand it another key, and it refuses any key
#                  whose fingerprint equals the published release key's. No agent. --test-pubkey-out FILE exports the throwaway PUBLIC key.
#   --real         Signs with the owner's ssh-agent. Needs --i-have-owner-authorization <sha256 of the scope file>, --scope-file, --ledger DIR,
#                  --expected-sha256, --expected-fingerprint, --pubkey (public) and SSH_AUTH_SOCK (the owner's agent, key already loaded).
#
# SCOPE FILE (KEY=VALUE; read once into a private copy that is hashed and parsed; a TRIPWIRE against mistakes, NOT an authentication):
#   scope_version=2 not_before/not_after (UTC, at most 24 h) script_sha256 common_sha256 identity_json_sha256 manifest_sha256
#   key_fingerprint namespace=redevops-qa-release@v1 build_commit procedure_commit
#
# bash 3.2 compatible. No `set -x`, no rm -r.
#
# Usage:
#   sign_manifest_sshsig.sh --manifest FILE --pubkey FILE.pub --expected-sha256 HEX [--expected-fingerprint SHA256:...]
#       [--dry-run | --self-test [--test-pubkey-out FILE] | --real --i-have-owner-authorization SHA --scope-file FILE --ledger DIR [--allow-reuse]]
#       [--sig-out FILE] [--record-out FILE]
#   sign_manifest_sshsig.sh --verify-only --manifest FILE --sig FILE --pubkey FILE.pub [--expected-fingerprint SHA256:...]

#
# ---- FIRST EXECUTABLE LINES (rounds 5 and 6): no external command and no function runs here; only reserved words, parameter expansion, assignments and
# builtins do, and PATH is replaced by a fixed value FIRST, so even a start that skips the re-exec resolves nothing through the caller's PATH.
# What is and is not guaranteed, by way of starting the script:
#   ./script          shebang '#!/bin/bash -p': privileged mode (BASH_ENV/ENV/SHELLOPTS/BASHOPTS/CDPATH/GLOBIGNORE ignored, exported functions NOT imported).
#                     Nothing of the caller's runs before this block; the script then re-executes itself through /usr/bin/env -i. RECOMMENDED.
#   bash -p script    same.
#   bash script, sh script   NOT privileged: BASH_ENV (bash) and SHELLOPTS+PS4, and an exported function named `builtin`, run BEFORE this block and cannot
#                     be prevented by it. The script refuses loudly when BASH_ENV/ENV is set in such a start, and re-executes itself as above otherwise.
#                     Use ./script.
# The re-exec marker is a POSITIONAL argument (--r0p-reexec) that is only trusted together with privileged mode; no environment variable is consulted
# or trusted, and a skipped re-exec still ends in r0p_scrub, which refuses any environment that is not the allowlist.
R0P_ORIG_PATH="${PATH:-}"
PATH=/usr/bin:/bin:/usr/sbin:/sbin
R0P_STARTED_OK=""
R0P_IS_REAL=""
for R0P_A in "$@"; do
  if [[ "$R0P_A" == --real ]]; then R0P_IS_REAL=1; fi
done
if [[ -n "$R0P_IS_REAL" && -n "${R0P_ALLOW_NONSYSTEM_TOOLS:-}" ]]; then   # R0P-TEST-PATCH-POINT-PRE
  builtin echo "ERROR: R0P_ALLOW_NONSYSTEM_TOOLS (a unit-test switch) is refused in --real [first block]" >&2
  builtin exit 1
fi
if [[ "${1:-}" == --r0p-reexec && "$-" == *p* ]]; then
  shift
  R0P_STARTED_OK=1
fi
if [[ -z "$R0P_STARTED_OK" ]]; then
  if [[ "$-" != *p* && -n "${BASH_ENV:-}${ENV:-}" ]]; then
    builtin echo "ERROR: BASH_ENV/ENV is set and the script was not started in privileged mode; start it as ./script (refusing)" >&2
    builtin exit 1
  fi
  IFS=$' \t\n'
  R0P_KEEP=()
  for R0P_V in R0P_ALLOW_NONSYSTEM_TOOLS; do
    if [[ -n "${!R0P_V+x}" ]]; then R0P_KEEP+=("$R0P_V=${!R0P_V}"); fi
  done
  for R0P_A in "$@"; do
    if [[ "$R0P_A" == --real && -n "${SSH_AUTH_SOCK:-}" ]]; then R0P_KEEP+=("SSH_AUTH_SOCK=$SSH_AUTH_SOCK"); fi
  done
  R0P_PATHV="$PATH"
  if [[ -z "$R0P_IS_REAL" && "${R0P_ALLOW_NONSYSTEM_TOOLS:-}" == stub-test ]]; then R0P_PATHV="$R0P_ORIG_PATH"; fi   # R0P-TEST-PATCH-POINT-PATH
  builtin exec 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- /usr/bin/env -i "PATH=$R0P_PATHV" "HOME=${HOME:-/nonexistent}" LANG=C LC_ALL=C "TERM=${TERM:-dumb}" \
       ${R0P_KEEP[@]+"${R0P_KEEP[@]}"} /bin/bash -p "${BASH_SOURCE[0]}" --r0p-reexec "$@"
fi
# the directory of the script without dirname/basename
R0P_SP="${BASH_SOURCE[0]}"
case "$R0P_SP" in
  */*) R0P_SD="${R0P_SP%/*}"; [[ -n "$R0P_SD" ]] || R0P_SD="/";;
  *) R0P_SD=".";;
esac
SELF_DIR="$(builtin cd "$R0P_SD" && builtin pwd -P)"
SELF_NAME="${R0P_SP##*/}"
SELF="$SELF_DIR/$SELF_NAME"
case "$-" in *x*) echo "ERROR: xtrace is on; refusing" >&2; exit 1;; esac
set +x
set -euo pipefail
umask 077

SCRIPT_VERSION="r0prime-sshsig-2"
NAMESPACE="redevops-qa-release@v1"
PRINCIPAL="redevops-qa-release"
# The PUBLISHED fingerprint of the existing release key (SIGNING_REQUEST.md, VERIFY.md; `ssh-keygen -lf` on its public key prints exactly this:
# "SHA256:" plus 43 characters). --self-test refuses a key whose fingerprint EQUALS it; --real records whether the key matches it.
RELEASE_KEY_FPR="SHA256:wUoE8wxKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA"

die() { echo "ERROR: $*" >&2; exit 1; }
. "$SELF_DIR/signing-common.sh"
# the owner's agent socket may be in the environment ONLY for --real; in every other mode its presence makes the script re-execute without it
EXTRA=" R0P_ALLOW_NONSYSTEM_TOOLS "
for a in "$@"; do if [ "$a" = "--real" ]; then EXTRA="$EXTRA SSH_AUTH_SOCK "; fi; done
r0p_scrub "$EXTRA" "$@"
r0p_fix_path ""
export LC_ALL=C
COMMON_SHA="$(r0p_sha "$SELF_DIR/signing-common.sh")"
usage() { sed -n -E '/^# Usage:/,/^([^#]|$)/p' "$SELF" | sed '$d' | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }

MANIFEST=""; PUBKEY=""; EXPECTED_SHA=""; EXPECTED_FPR=""; SIG_OUT=""; RECORD_OUT=""; SIG_IN=""; TEST_PUB_OUT=""
MODE_DRY=0; MODE_VERIFY=0; MODE_SELF=0; MODE_REAL=0; AUTH_SHA=""; SCOPE_FILE=""; LEDGER=""; ALLOW_REUSE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --manifest) [ $# -ge 2 ] || die "--manifest needs a value"; MANIFEST="$2"; shift 2;;
    --pubkey) [ $# -ge 2 ] || die "--pubkey needs a value"; PUBKEY="$2"; shift 2;;
    --expected-sha256) [ $# -ge 2 ] || die "--expected-sha256 needs a value"; EXPECTED_SHA="$2"; shift 2;;
    --expected-fingerprint) [ $# -ge 2 ] || die "--expected-fingerprint needs a value"; EXPECTED_FPR="$2"; shift 2;;
    --sig-out) [ $# -ge 2 ] || die "--sig-out needs a value"; SIG_OUT="$2"; shift 2;;
    --record-out) [ $# -ge 2 ] || die "--record-out needs a value"; RECORD_OUT="$2"; shift 2;;
    --sig) [ $# -ge 2 ] || die "--sig needs a value"; SIG_IN="$2"; shift 2;;
    --test-pubkey-out) [ $# -ge 2 ] || die "--test-pubkey-out needs a value"; TEST_PUB_OUT="$2"; shift 2;;
    --i-have-owner-authorization) [ $# -ge 2 ] || die "--i-have-owner-authorization needs the sha256 of the scope file"; AUTH_SHA="$2"; shift 2;;
    --scope-file) [ $# -ge 2 ] || die "--scope-file needs a value"; SCOPE_FILE="$2"; shift 2;;
    --ledger) [ $# -ge 2 ] || die "--ledger needs a value"; LEDGER="$2"; shift 2;;
    --allow-reuse) ALLOW_REUSE=1; shift;;
    --dry-run) MODE_DRY=1; shift;;
    --verify-only) MODE_VERIFY=1; shift;;
    --self-test) MODE_SELF=1; shift;;
    --real) MODE_REAL=1; shift;;
    -h|--help) usage 0;;
    *) echo "unknown argument: $1" >&2; usage 2;;
  esac
done
[ $((MODE_DRY + MODE_VERIFY + MODE_SELF + MODE_REAL)) -le 1 ] || die "give at most one of --dry-run, --verify-only, --self-test, --real"
if [ $((MODE_DRY + MODE_VERIFY + MODE_SELF + MODE_REAL)) -eq 0 ]; then MODE_DRY=1; fi
if [ "$MODE_REAL" = 1 ]; then MODE=real; elif [ "$MODE_SELF" = 1 ]; then MODE=self-test; elif [ "$MODE_VERIFY" = 1 ]; then MODE=verify-only; else MODE=dry-run; fi
if [ "$MODE" = real ]; then r0p_no_test_switch; fi      # the unit-test switch is refused in --real (round 5, R5-02)

sha() { r0p_sha "$1"; }
abspath() { ( cd "$(dirname "$1")" 2>/dev/null && printf '%s/%s' "$(pwd -P)" "$(basename "$1")" ); }
KEYGEN="$(command -v ssh-keygen || true)"; [ -n "$KEYGEN" ] || die "ssh-keygen not found"
SSHADD="$(command -v ssh-add || true)"
if [ "$R0P_STUBS" = 0 ]; then
  [ "$KEYGEN" = /usr/bin/ssh-keygen ] || die "ssh-keygen must be /usr/bin/ssh-keygen (got $KEYGEN)"
  if [ "$MODE" = real ]; then [ "$SSHADD" = /usr/bin/ssh-add ] || die "ssh-add must be /usr/bin/ssh-add (got ${SSHADD:-none})"; fi
fi
r0p_mktmp
trap 'r0p_tmp_cleanup' EXIT
trap 'exit 130' INT TERM HUP

# ---------------------------------------------------------------- inputs
[ -n "$MANIFEST" ] && [ -f "$MANIFEST" ] && [ ! -L "$MANIFEST" ] || die "--manifest must be an existing regular file (not a symlink)"
MANIFEST="$(abspath "$MANIFEST")"; MDIR="$(dirname "$MANIFEST")"
if [ "$MODE" = self-test ]; then
  [ -z "$PUBKEY" ] || die "--self-test generates its own throwaway key; --pubkey is not accepted"
  mkdir "$R0P_TMP/selftest"; chmod 700 "$R0P_TMP/selftest"
  "$KEYGEN" -q -t ed25519 -N '' -C signproc-TEST-KEY -f "$R0P_TMP/selftest/key" >/dev/null 2>&1 || die "could not generate the throwaway test key"
  PUBKEY="$R0P_TMP/selftest/key.pub"
else
  [ -n "$PUBKEY" ] && [ -f "$PUBKEY" ] || die "--pubkey must be the PUBLIC key file"
  [ -z "$TEST_PUB_OUT" ] || die "--test-pubkey-out belongs to --self-test"
fi
# public key: one line, ssh-ed25519, no options; a file that holds a private key is refused outright
if grep -q 'PRIVATE KEY' "$PUBKEY" 2>/dev/null; then die "--pubkey contains a private key; refusing (give the .pub file; this script never handles private keys)"; fi
[ "$(wc -l <"$PUBKEY" | tr -d ' ')" -le 1 ] || die "--pubkey must be a single line"
PUBLINE="$(head -1 "$PUBKEY")"
[[ "$PUBLINE" =~ ^ssh-ed25519\ [A-Za-z0-9+/]{68}(\ [[:print:]]{0,200})?$ ]] || die "--pubkey must be one 'ssh-ed25519 <base64> [comment]' line (no options, no certificate, no other key type)"
FPR_LINE="$("$KEYGEN" -lf "$PUBKEY" 2>/dev/null)" || die "ssh-keygen could not read the public key"
FPR="$(echo "$FPR_LINE" | awk '{print $2}')"
[[ "$FPR" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] || die "unexpected fingerprint format"
if [ "$MODE" = self-test ]; then
  [ "$FPR" != "$RELEASE_KEY_FPR" ] || die "--self-test refuses the release key (its fingerprint equals the published release fingerprint)"
fi
if [ -n "$EXPECTED_FPR" ]; then
  [[ "$EXPECTED_FPR" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] || die "--expected-fingerprint must look like SHA256:<43 chars>"
  [ "$FPR" = "$EXPECTED_FPR" ] || die "the public key's fingerprint ($FPR) is not the expected one ($EXPECTED_FPR)"
fi
ACTUAL_SHA="$(sha "$MANIFEST")"

if [ "$MODE" != verify-only ]; then
  [[ "$EXPECTED_SHA" =~ ^[0-9a-fA-F]{64}$ ]] || die "--expected-sha256 (the digest of the manifest, obtained out of band) must be 64 hex chars"
  EXPECTED_SHA="$(echo "$EXPECTED_SHA" | tr 'A-F' 'a-f')"
  [ "$ACTUAL_SHA" = "$EXPECTED_SHA" ] || die "the manifest's sha256 ($ACTUAL_SHA) is not the expected digest ($EXPECTED_SHA); nothing was signed"
  if [ "$MODE" != dry-run ]; then
    [ -n "$SIG_OUT" ] || die "--sig-out FILE is required (the signature is written there, outside the evidence directory)"
    SIG_OUT="$(abspath "$SIG_OUT")"
    case "$SIG_OUT" in "$MDIR"/*) die "--sig-out is inside the manifest's directory ($MDIR): the signature must not be added to the evidence set";; esac
    [ ! -e "$SIG_OUT" ] || die "--sig-out already exists; refusing to overwrite"
    [ -d "$(dirname "$SIG_OUT")" ] || die "the directory of --sig-out does not exist"
    if [ -n "$RECORD_OUT" ]; then
      RECORD_OUT="$(abspath "$RECORD_OUT")"
      case "$RECORD_OUT" in "$MDIR"/*) die "--record-out is inside the manifest's directory";; esac
      [ ! -e "$RECORD_OUT" ] || die "--record-out already exists; refusing to overwrite"
    fi
    if [ -n "$TEST_PUB_OUT" ]; then
      TEST_PUB_OUT="$(abspath "$TEST_PUB_OUT")"
      case "$TEST_PUB_OUT" in "$MDIR"/*) die "--test-pubkey-out is inside the manifest's directory";; esac
      [ ! -e "$TEST_PUB_OUT" ] || die "--test-pubkey-out already exists; refusing to overwrite"
    fi
  fi
else
  [ -n "$SIG_IN" ] && [ -f "$SIG_IN" ] || die "--verify-only needs --sig FILE"
fi

# ---------------------------------------------------------------- mode policy
IDJSON="$SELF_DIR/../identity.json"
if [ "$MODE" = real ]; then
  [ -n "$EXPECTED_FPR" ] || die "--real needs --expected-fingerprint (the fingerprint the owner published for the release key)"
  [ -n "${SSH_AUTH_SOCK:-}" ] || die "--real needs the owner's ssh-agent (SSH_AUTH_SOCK in the environment); the script never reads a key file"
  [ -S "$SSH_AUTH_SOCK" ] || die "SSH_AUTH_SOCK is not a socket"
  [ "$(basename "$MANIFEST")" = release-manifest.json ] || die "--real signs only the top-level release-manifest.json of an evidence set"
  [[ "$AUTH_SHA" =~ ^[0-9a-fA-F]{64}$ ]] || die "--real needs --i-have-owner-authorization <sha256 of the scope file>"
  AUTH_SHA="$(echo "$AUTH_SHA" | tr 'A-F' 'a-f')"
  [ -n "$SCOPE_FILE" ] || die "--real needs --scope-file FILE"
  r0p_scope_load "$SCOPE_FILE" "$AUTH_SHA" scope_version not_before not_after script_sha256 common_sha256 identity_json_sha256 manifest_sha256 \
    key_fingerprint namespace build_commit procedure_commit
  [ "$S_scope_version" = 2 ] || die "scope file: scope_version must be 2"
  r0p_window_check "$S_not_before" "$S_not_after"
  [ "$S_script_sha256" = "$(sha "$SELF")" ] || die "scope file: script_sha256 does not match this script"
  [ "$S_common_sha256" = "$COMMON_SHA" ] || die "scope file: common_sha256 does not match signing-common.sh"
  [ "$S_identity_json_sha256" = "$(sha "$IDJSON")" ] || die "scope file: identity_json_sha256 does not match packaging/identity.json"
  [ "$S_manifest_sha256" = "$ACTUAL_SHA" ] || die "scope file: manifest_sha256 does not match the manifest"
  [ "$S_key_fingerprint" = "$FPR" ] || die "scope file: key_fingerprint does not match the public key"
  [ "$S_namespace" = "$NAMESPACE" ] || die "scope file: namespace must be $NAMESPACE"
  [[ "$S_build_commit" =~ ^[0-9a-f]{40}$ ]] && [[ "$S_procedure_commit" =~ ^[0-9a-f]{40}$ ]] || die "scope file: build_commit and procedure_commit must be 40-hex git commit ids"
  r0p_ledger_check "$LEDGER" "$AUTH_SHA" "$ALLOW_REUSE"
else
  [ -z "$AUTH_SHA$SCOPE_FILE$LEDGER" ] && [ "$ALLOW_REUSE" = 0 ] || die "--i-have-owner-authorization/--scope-file/--ledger/--allow-reuse are for --real only"
  if [ "$MODE" = self-test ]; then [ -z "${SSH_AUTH_SOCK:-}" ] || die "--self-test never uses an agent (SSH_AUTH_SOCK is set)"; fi
fi

if [ "$R0P_STUBS" = 1 ]; then KEYGEN_VERSION="(stub tools)"; else
  SSH_BIN="$(dirname "$KEYGEN")/ssh"
  if [ -x "$SSH_BIN" ]; then KEYGEN_VERSION="$("$SSH_BIN" -V 2>&1 | head -1 || true)"; else KEYGEN_VERSION="(OpenSSH version not determined)"; fi
fi
echo "== $SCRIPT_VERSION  mode: $MODE"
echo "manifest:  $MANIFEST sha256=$ACTUAL_SHA"
echo "namespace: $NAMESPACE   principal: $PRINCIPAL"
if [ "$FPR" = "$RELEASE_KEY_FPR" ]; then RELEASE_MATCH="MATCHES"; else RELEASE_MATCH="DOES NOT match"; fi
echo "public key fingerprint: $FPR (this key $RELEASE_MATCH the published release fingerprint)"

# ---------------------------------------------------------------- dry-run: print only
if [ "$MODE" = dry-run ]; then
  echo "expected digest matches (a read). DRY-RUN: nothing is signed, no agent is contacted, nothing is created. Commands that --real would run:"
  echo "+ ssh-add -l   # the owner's agent must list $FPR; otherwise the run is refused (no fallback to a key file)"
  echo "+ HOME=<throwaway> SSH_ASKPASS=/usr/bin/false ssh-keygen -Y sign -U -n $NAMESPACE -f <private temp copy of $(printf '%q' "$PUBKEY")> < $(printf '%q' "$MANIFEST") > <sig-out>   # the owner's ssh-agent signs; no file is added to $MDIR"
  echo "+ ssh-keygen -Y verify -f <allowed_signers: $PRINCIPAL namespaces=\"$NAMESPACE\" <public key>> -I $PRINCIPAL -n $NAMESPACE -s <sig-out> < $(printf '%q' "$MANIFEST")"
  exit 0
fi

# ---------------------------------------------------------------- sign and verify the SAME bytes (a private copy), key material only as the public key
cp "$MANIFEST" "$R0P_TMP/manifest"
[ "$(sha "$R0P_TMP/manifest")" = "$ACTUAL_SHA" ] || die "the manifest changed while it was being copied"
printf '%s namespaces="%s" %s\n' "$PRINCIPAL" "$NAMESPACE" "$PUBLINE" >"$R0P_TMP/allowed_signers"
verify_sig() { # sigfile manifestfile -> 0 if good; output in $R0P_TMP/verify.out
  "$KEYGEN" -Y verify -f "$R0P_TMP/allowed_signers" -I "$PRINCIPAL" -n "$NAMESPACE" -s "$1" <"$2" >"$R0P_TMP/verify.out" 2>&1
}

if [ "$MODE" = verify-only ]; then
  cp "$SIG_IN" "$R0P_TMP/sig"
  if verify_sig "$R0P_TMP/sig" "$R0P_TMP/manifest"; then
    echo "VERIFIED: $(head -1 "$R0P_TMP/verify.out")"
    echo "(manifest sha256 $ACTUAL_SHA; fingerprint $FPR; the signature carries no time and proves nothing about who holds the key)"
    exit 0
  fi
  echo "NOT VERIFIED: $(head -1 "$R0P_TMP/verify.out")" >&2
  exit 5
fi

echo "signing: the manifest is streamed on stdin; the signature is read from stdout; nothing is written to $MDIR"
if [ "$MODE" = real ]; then
  # R4-01: the agent must hold the key; a throwaway HOME and no askpass/DISPLAY mean ssh-keygen cannot read ~/.ssh or ask for anything;
  # the public key is a COPY in a private directory, so no private key file sits beside it
  mkdir "$R0P_TMP/home" "$R0P_TMP/pub"; chmod 700 "$R0P_TMP/home" "$R0P_TMP/pub"
  cp "$PUBKEY" "$R0P_TMP/pub/release.pub"
  [ -z "$(ls -A "$R0P_TMP/pub" | grep -v '^release.pub$' || true)" ] || die "unexpected files beside the public key copy"
  AGENT_LIST="$(env -i "PATH=$PATH" "HOME=$R0P_TMP/home" "SSH_AUTH_SOCK=$SSH_AUTH_SOCK" "$SSHADD" -l 2>/dev/null)" || die "ssh-add -l could not list the owner's agent (is it running, and is the key loaded?); refusing: this script never falls back to a key file"
  echo "$AGENT_LIST" | awk '{print $2}' | grep -qx -F "$FPR" || die "the owner's agent does not hold the key $FPR; refusing (no fallback to a private key file, no prompt)"
  r0p_ledger_claim "$LEDGER" "$AUTH_SHA" "$ALLOW_REUSE" real "script=$(sha "$SELF") manifest=$ACTUAL_SHA key=$FPR"
  echo "+ ssh-keygen -Y sign -U -n $NAMESPACE -f <private copy of the public key>   # the owner's ssh-agent signs"
  env -i "PATH=$PATH" "HOME=$R0P_TMP/home" "SSH_AUTH_SOCK=$SSH_AUTH_SOCK" SSH_ASKPASS=/usr/bin/false SSH_ASKPASS_REQUIRE=never \
    "$KEYGEN" -Y sign -U -n "$NAMESPACE" -f "$R0P_TMP/pub/release.pub" <"$R0P_TMP/manifest" >"$R0P_TMP/sig" 2>"$R0P_TMP/sign.err" \
    || { cat "$R0P_TMP/sign.err" >&2; ( r0p_ledger_append "$LEDGER" "$AUTH_SHA" real ended "rc=sign-failed" ) 2>/dev/null; die "ssh-keygen -Y sign failed (the agent did not sign)"; }
else
  echo "+ ssh-keygen -Y sign -n $NAMESPACE -f <throwaway test key generated by this run>   # SELF-TEST"
  "$KEYGEN" -Y sign -n "$NAMESPACE" -f "$R0P_TMP/selftest/key" <"$R0P_TMP/manifest" >"$R0P_TMP/sig" 2>"$R0P_TMP/sign.err" || { cat "$R0P_TMP/sign.err" >&2; die "ssh-keygen -Y sign failed"; }
fi
[ -s "$R0P_TMP/sig" ] || die "the signature is empty"
head -1 "$R0P_TMP/sig" | grep -q '^-----BEGIN SSH SIGNATURE-----$' || die "the output is not an SSH signature"
# verify before releasing, against the public key supplied (never against anything found elsewhere)
verify_sig "$R0P_TMP/sig" "$R0P_TMP/manifest" || { cat "$R0P_TMP/verify.out" >&2; die "the new signature does not verify; not released"; }
[ "$(sha "$MANIFEST")" = "$ACTUAL_SHA" ] || die "the manifest changed during signing; not released"
cp "$R0P_TMP/sig" "$SIG_OUT"
if [ -n "$TEST_PUB_OUT" ]; then cp "$PUBKEY" "$TEST_PUB_OUT"; fi
if [ "$MODE" = real ]; then r0p_ledger_append "$LEDGER" "$AUTH_SHA" real ended "rc=0 signature=$(sha "$SIG_OUT")"; fi
SIG_SHA="$(sha "$SIG_OUT")"
REC="script: $(basename "$SELF") $SCRIPT_VERSION sha256=$(sha "$SELF") common sha256=$COMMON_SHA
mode: $MODE$( [ "$MODE" = self-test ] && echo ' (THROWAWAY KEY generated by the run and deleted: not a release signature)')
manifest: $(basename "$MANIFEST") sha256=$ACTUAL_SHA
namespace: $NAMESPACE
public key fingerprint: $FPR (published release fingerprint: $RELEASE_MATCH)
signature file: $(basename "$SIG_OUT") sha256=$SIG_SHA
verification: $(head -1 "$R0P_TMP/verify.out")
ssh-keygen: $KEYGEN_VERSION
caveats: an SSHSIG signature carries no time; the date of signing is only what this record and the owner state; it does not show who holds the key."
echo "$REC"
if [ -n "$RECORD_OUT" ]; then printf '%s\n' "$REC" >"$RECORD_OUT"; echo "record written to $(basename "$RECORD_OUT")"; fi
echo "signature written to: $SIG_OUT"
