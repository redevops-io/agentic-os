#!/usr/bin/env bash
# sign_manifest_sshsig.sh  (script version r0prime-sshsig-1)
#
# Sign (or only verify) the top-level evidence manifest of an R0-prime evidence set with an OpenSSH signature (SSHSIG),
# namespace redevops-qa-release@v1, WITHOUT adding any file to the evidence directory: the manifest is streamed to
# `ssh-keygen -Y sign` on stdin and the signature is read from its stdout into a file OUTSIDE the evidence directory.
# Procedure and caveats: packaging/signing/SSHSIG-manifest-signing.md.
#
# THIS SCRIPT NEVER READS, COPIES OR EXPORTS A PRIVATE KEY. In --real mode only the owner's ssh-agent signs: the script is
# given the PUBLIC key file (-f <public key>), and it refuses a file that contains a private key. It never touches ~/.ssh.
#
# MODES (exactly one; default --dry-run)
#   --dry-run    DEFAULT. Checks the manifest sha256 against --expected-sha256, parses the public key, prints the exact
#                commands. Does not contact an agent, creates nothing, signs nothing.
#   --verify-only  Verifies an existing signature (--sig FILE) of --manifest with --pubkey. Needs no secret; this is what a
#                reviewer or third party runs.
#   --self-test  Signs with a THROWAWAY test key (--test-private-key FILE; its public part must carry the comment
#                signproc-TEST-KEY and must not be the release key). No agent. For the unit tests of the mechanics only.
#   --real       Signs with the owner's ssh-agent. Needs --i-have-owner-authorization <sha256 of the scope file>, --scope-file,
#                --expected-sha256, --expected-fingerprint and --pubkey (public), and SSH_AUTH_SOCK in the environment (the
#                owner's agent; the key must already be loaded by the owner, or be prompted for by the agent).
#
# SCOPE FILE (KEY=VALUE; unknown or duplicate keys refused): scope_version=1, valid_from=YYYY-MM-DD, valid_until=YYYY-MM-DD,
#   script_sha256=<this file>, manifest_sha256=<digest to be signed>, key_fingerprint=SHA256:<43 chars>, namespace=redevops-qa-release@v1
#
# bash 3.2 compatible. No `set -x`, no rm -rf.
#
# Usage:
#   sign_manifest_sshsig.sh --manifest FILE --pubkey FILE.pub --expected-sha256 HEX [--expected-fingerprint SHA256:...]
#       [--dry-run | --self-test --test-private-key FILE | --real --i-have-owner-authorization SHA --scope-file FILE]
#       [--sig-out FILE] [--record-out FILE]
#   sign_manifest_sshsig.sh --verify-only --manifest FILE --sig FILE --pubkey FILE.pub [--expected-fingerprint SHA256:...]
case "$-" in *x*) echo "ERROR: xtrace is on; refusing" >&2; exit 1;; esac
set +x
set -euo pipefail
umask 077

SCRIPT_VERSION="r0prime-sshsig-1"
NAMESPACE="redevops-qa-release@v1"
PRINCIPAL="redevops-qa-release"
# the PUBLIC fingerprint of the existing release key (published by the owner); only used to refuse it in --self-test
RELEASE_KEY_FPR="SHA256:wUoE8xKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"

if [ "${R0P_SCRUBBED:-}" != 1 ]; then
  KEEP=()
  # the owner's agent socket is passed on ONLY for --real (checked below after re-exec); never otherwise
  WANT_AGENT=0; for a in "$@"; do [ "$a" = "--real" ] && WANT_AGENT=1; done
  if [ "$WANT_AGENT" = 1 ] && [ -n "${SSH_AUTH_SOCK:-}" ]; then KEEP+=("SSH_AUTH_SOCK=$SSH_AUTH_SOCK"); fi
  for v in $(compgen -A export | grep '^R0P_' || true); do KEEP+=("$v=${!v}"); done
  exec env -i "PATH=${PATH:-/usr/bin:/bin}" "HOME=${HOME:-/nonexistent}" LANG=C LC_ALL=C "TERM=${TERM:-dumb}" \
       ${KEEP[@]+"${KEEP[@]}"} R0P_SCRUBBED=1 "${BASH:-/bin/bash}" "$SELF" "$@"
fi
export LC_ALL=C

die() { echo "ERROR: $*" >&2; exit 1; }
usage() { sed -n '/^# Usage:/,/^case /p' "$SELF" | sed '$d' | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }

MANIFEST=""; PUBKEY=""; EXPECTED_SHA=""; EXPECTED_FPR=""; SIG_OUT=""; RECORD_OUT=""; SIG_IN=""
MODE_DRY=0; MODE_VERIFY=0; MODE_SELF=0; MODE_REAL=0; TESTKEY=""; AUTH_SHA=""; SCOPE_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --manifest) [ $# -ge 2 ] || die "--manifest needs a value"; MANIFEST="$2"; shift 2;;
    --pubkey) [ $# -ge 2 ] || die "--pubkey needs a value"; PUBKEY="$2"; shift 2;;
    --expected-sha256) [ $# -ge 2 ] || die "--expected-sha256 needs a value"; EXPECTED_SHA="$2"; shift 2;;
    --expected-fingerprint) [ $# -ge 2 ] || die "--expected-fingerprint needs a value"; EXPECTED_FPR="$2"; shift 2;;
    --sig-out) [ $# -ge 2 ] || die "--sig-out needs a value"; SIG_OUT="$2"; shift 2;;
    --record-out) [ $# -ge 2 ] || die "--record-out needs a value"; RECORD_OUT="$2"; shift 2;;
    --sig) [ $# -ge 2 ] || die "--sig needs a value"; SIG_IN="$2"; shift 2;;
    --test-private-key) [ $# -ge 2 ] || die "--test-private-key needs a value"; TESTKEY="$2"; shift 2;;
    --i-have-owner-authorization) [ $# -ge 2 ] || die "--i-have-owner-authorization needs the sha256 of the scope file"; AUTH_SHA="$2"; shift 2;;
    --scope-file) [ $# -ge 2 ] || die "--scope-file needs a value"; SCOPE_FILE="$2"; shift 2;;
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

sha() { shasum -a 256 "$1" | awk '{print $1}'; }
abspath() { ( cd "$(dirname "$1")" 2>/dev/null && printf '%s/%s' "$(pwd -P)" "$(basename "$1")" ); }
KEYGEN="$(command -v ssh-keygen || true)"; [ -n "$KEYGEN" ] || die "ssh-keygen not found"
case "$KEYGEN" in /usr/bin/*|/bin/*|/usr/local/bin/*|/opt/homebrew/bin/*) ;; *) [ "${R0P_ALLOW_NONSYSTEM_TOOLS:-}" = stub-test ] || die "ssh-keygen resolves to an unexpected location: $KEYGEN";; esac

# ---------------------------------------------------------------- inputs
[ -n "$MANIFEST" ] && [ -f "$MANIFEST" ] && [ ! -L "$MANIFEST" ] || die "--manifest must be an existing regular file (not a symlink)"
[ -n "$PUBKEY" ] && [ -f "$PUBKEY" ] || die "--pubkey must be the PUBLIC key file"
MANIFEST="$(abspath "$MANIFEST")"; MDIR="$(dirname "$MANIFEST")"
# public key: one line, ssh-ed25519, no options; a file that holds a private key is refused outright
if grep -q 'PRIVATE KEY' "$PUBKEY" 2>/dev/null; then die "--pubkey contains a private key; refusing (give the .pub file; this script never handles private keys)"; fi
[ "$(wc -l <"$PUBKEY" | tr -d ' ')" -le 1 ] || die "--pubkey must be a single line"
PUBLINE="$(head -1 "$PUBKEY")"
[[ "$PUBLINE" =~ ^ssh-ed25519\ [A-Za-z0-9+/]{68}(\ [[:print:]]{0,200})?$ ]] || die "--pubkey must be one 'ssh-ed25519 <base64> [comment]' line (no options, no certificate, no other key type)"
FPR_LINE="$("$KEYGEN" -lf "$PUBKEY" 2>/dev/null)" || die "ssh-keygen could not read the public key"
FPR="$(echo "$FPR_LINE" | awk '{print $2}')"
[[ "$FPR" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] || die "unexpected fingerprint format"
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
  fi
else
  [ -n "$SIG_IN" ] && [ -f "$SIG_IN" ] || die "--verify-only needs --sig FILE"
fi

# ---------------------------------------------------------------- mode policy
if [ "$MODE" = real ]; then
  [ -n "$EXPECTED_FPR" ] || die "--real needs --expected-fingerprint (the fingerprint the owner published for the release key)"
  [ -n "${SSH_AUTH_SOCK:-}" ] || die "--real needs the owner's ssh-agent (SSH_AUTH_SOCK in the environment); the script never reads a key file"
  [ -S "$SSH_AUTH_SOCK" ] || die "SSH_AUTH_SOCK is not a socket"
  [ "$(basename "$MANIFEST")" = release-manifest.json ] || die "--real signs only the top-level release-manifest.json of an evidence set"
  [[ "$AUTH_SHA" =~ ^[0-9a-fA-F]{64}$ ]] || die "--real needs --i-have-owner-authorization <sha256 of the scope file>"
  AUTH_SHA="$(echo "$AUTH_SHA" | tr 'A-F' 'a-f')"
  [ -n "$SCOPE_FILE" ] && [ -f "$SCOPE_FILE" ] && [ ! -L "$SCOPE_FILE" ] || die "--real needs --scope-file FILE"
  [ "$(sha "$SCOPE_FILE")" = "$AUTH_SHA" ] || die "the scope file's sha256 does not equal the value given to --i-have-owner-authorization"
  S_scope_version=""; S_valid_from=""; S_valid_until=""; S_script_sha256=""; S_manifest_sha256=""; S_key_fingerprint=""; S_namespace=""
  SEEN=" "
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue;; esac
    key="${line%%=*}"; val="${line#*=}"
    [ "$key" != "$line" ] || die "scope file: line without '='"
    case "$val" in *[[:cntrl:]]*) die "scope file: control character in a value";; esac
    case "$SEEN" in *" $key "*) die "scope file: duplicate key $key";; esac
    SEEN="$SEEN$key "
    case "$key" in
      scope_version|valid_from|valid_until|script_sha256|manifest_sha256|key_fingerprint|namespace) eval "S_$key=\$val";;
      *) die "scope file: unknown key '$key'";;
    esac
  done < "$SCOPE_FILE"
  [ "$S_scope_version" = 1 ] || die "scope file: scope_version must be 1"
  [[ "$S_valid_from" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && [[ "$S_valid_until" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "scope file: dates must be YYYY-MM-DD"
  TODAY="$(date -u +%Y-%m-%d)"
  { [ "$TODAY" = "$S_valid_from" ] || [[ "$TODAY" > "$S_valid_from" ]]; } && { [ "$TODAY" = "$S_valid_until" ] || [[ "$TODAY" < "$S_valid_until" ]]; } \
    || die "scope file: today (UTC $TODAY) is outside the authorized window $S_valid_from .. $S_valid_until"
  [ "$S_script_sha256" = "$(sha "$SELF")" ] || die "scope file: script_sha256 does not match this script"
  [ "$S_manifest_sha256" = "$ACTUAL_SHA" ] || die "scope file: manifest_sha256 does not match the manifest"
  [ "$S_key_fingerprint" = "$FPR" ] || die "scope file: key_fingerprint does not match the public key"
  [ "$S_namespace" = "$NAMESPACE" ] || die "scope file: namespace must be $NAMESPACE"
elif [ "$MODE" = self-test ]; then
  [ -n "$TESTKEY" ] && [ -f "$TESTKEY" ] || die "--self-test needs --test-private-key FILE (a throwaway key made for the test)"
  [ -z "$AUTH_SHA$SCOPE_FILE" ] || die "--i-have-owner-authorization/--scope-file are for --real only"
  case "$PUBLINE" in *" signproc-TEST-KEY") ;; *) die "--self-test accepts only a public key whose comment is signproc-TEST-KEY (a throwaway key)";; esac
  [ "$FPR" != "$RELEASE_KEY_FPR" ] || die "--self-test refuses the release key"
  TESTFPR="$("$KEYGEN" -lf "$TESTKEY" 2>/dev/null | awk '{print $2}')" || true
  [ "$TESTFPR" = "$FPR" ] || die "--test-private-key does not belong to --pubkey"
  [ -z "${SSH_AUTH_SOCK:-}" ] || die "--self-test never uses an agent (SSH_AUTH_SOCK is set)"
else
  [ -z "$AUTH_SHA$SCOPE_FILE" ] || die "--i-have-owner-authorization/--scope-file are for --real only"
fi

SSH_BIN="$(dirname "$KEYGEN")/ssh"
if [ -x "$SSH_BIN" ]; then KEYGEN_VERSION="$("$SSH_BIN" -V 2>&1 | head -1 || true)"; else KEYGEN_VERSION="(OpenSSH version not determined)"; fi
KEYGEN_VERSION="$KEYGEN_VERSION [ssh-keygen: $(basename "$KEYGEN")]"
q() { local s="" a; for a in "$@"; do s="$s$(printf '%q' "$a") "; done; printf '%s' "${s% }"; }
echo "== $SCRIPT_VERSION  mode: $MODE"
echo "manifest:  $MANIFEST sha256=$ACTUAL_SHA"
echo "namespace: $NAMESPACE   principal: $PRINCIPAL"
echo "public key fingerprint: $FPR"

# ---------------------------------------------------------------- dry-run: print only
if [ "$MODE" = dry-run ]; then
  echo "expected digest matches (a read). DRY-RUN: nothing is signed, no agent is contacted, nothing is created. Commands that --real would run:"
  echo "+ ssh-keygen -Y sign -n $NAMESPACE -f $(printf '%q' "$PUBKEY") < $(printf '%q' "$MANIFEST") > <sig-out>   # the owner's ssh-agent signs; no file is added to $MDIR"
  echo "+ ssh-keygen -Y verify -f <allowed_signers: $PRINCIPAL namespaces=\"$NAMESPACE\" <public key>> -I $PRINCIPAL -n $NAMESPACE -s <sig-out> < $(printf '%q' "$MANIFEST")"
  exit 0
fi

# ---------------------------------------------------------------- private temp area (outside the evidence directory)
TMPD="$(mktemp -d "${TMPDIR:-/tmp}/r0p-sshsig.XXXXXX")" || die "cannot create a private temp dir"
chmod 700 "$TMPD"
cleanup() {
  local rc=$?
  set +e
  rm -f "$TMPD/manifest" "$TMPD/allowed_signers" "$TMPD/sig" "$TMPD/verify.out" "$TMPD/sign.err"
  rmdir "$TMPD" 2>/dev/null
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP
# sign and verify the SAME bytes: a private copy, hashed after copying
cp "$MANIFEST" "$TMPD/manifest"
[ "$(sha "$TMPD/manifest")" = "$ACTUAL_SHA" ] || die "the manifest changed while it was being copied"
printf '%s namespaces="%s" %s\n' "$PRINCIPAL" "$NAMESPACE" "$PUBLINE" >"$TMPD/allowed_signers"

verify_sig() { # sigfile manifestfile -> 0 if good; output in $TMPD/verify.out
  "$KEYGEN" -Y verify -f "$TMPD/allowed_signers" -I "$PRINCIPAL" -n "$NAMESPACE" -s "$1" <"$2" >"$TMPD/verify.out" 2>&1
}

if [ "$MODE" = verify-only ]; then
  cp "$SIG_IN" "$TMPD/sig"
  if verify_sig "$TMPD/sig" "$TMPD/manifest"; then
    echo "VERIFIED: $(head -1 "$TMPD/verify.out")"
    echo "(manifest sha256 $ACTUAL_SHA; fingerprint $FPR; the signature carries no time and proves nothing about who holds the key)"
    exit 0
  fi
  echo "NOT VERIFIED: $(head -1 "$TMPD/verify.out")" >&2
  exit 5
fi

# ---------------------------------------------------------------- sign (stdin -> stdout)
echo "signing: the manifest is streamed on stdin; the signature is read from stdout; nothing is written to $MDIR"
if [ "$MODE" = real ]; then
  echo "+ ssh-keygen -Y sign -n $NAMESPACE -f <public key file>   # the owner's ssh-agent signs"
  "$KEYGEN" -Y sign -n "$NAMESPACE" -f "$PUBKEY" <"$TMPD/manifest" >"$TMPD/sig" 2>"$TMPD/sign.err" || { cat "$TMPD/sign.err" >&2; die "ssh-keygen -Y sign failed (is the key loaded in the owner's agent?)"; }
else
  echo "+ ssh-keygen -Y sign -n $NAMESPACE -f <throwaway test key>   # SELF-TEST"
  "$KEYGEN" -Y sign -n "$NAMESPACE" -f "$TESTKEY" <"$TMPD/manifest" >"$TMPD/sig" 2>"$TMPD/sign.err" || { cat "$TMPD/sign.err" >&2; die "ssh-keygen -Y sign failed"; }
fi
[ -s "$TMPD/sig" ] || die "the signature is empty"
head -1 "$TMPD/sig" | grep -q '^-----BEGIN SSH SIGNATURE-----$' || die "the output is not an SSH signature"
# verify before releasing, against the public key supplied (never against anything found elsewhere)
verify_sig "$TMPD/sig" "$TMPD/manifest" || { cat "$TMPD/verify.out" >&2; die "the new signature does not verify; not released"; }
[ "$(sha "$MANIFEST")" = "$ACTUAL_SHA" ] || die "the manifest changed during signing; not released"
cp "$TMPD/sig" "$SIG_OUT"
SIG_SHA="$(sha "$SIG_OUT")"
REC="script: $(basename "$SELF") $SCRIPT_VERSION sha256=$(sha "$SELF")
mode: $MODE$( [ "$MODE" = self-test ] && echo ' (THROWAWAY KEY: not a release signature)')
manifest: $(basename "$MANIFEST") sha256=$ACTUAL_SHA
namespace: $NAMESPACE
public key fingerprint: $FPR
signature file: $(basename "$SIG_OUT") sha256=$SIG_SHA
verification: $(head -1 "$TMPD/verify.out")
ssh-keygen: $KEYGEN_VERSION
caveats: an SSHSIG signature carries no time; the date of signing is only what this record and the owner state; it does not show who holds the key."
echo "$REC"
if [ -n "$RECORD_OUT" ]; then printf '%s\n' "$REC" >"$RECORD_OUT"; echo "record written to $(basename "$RECORD_OUT")"; fi
echo "signature written to: $SIG_OUT"
