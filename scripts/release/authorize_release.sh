#!/bin/bash -p
# authorize_release.sh: the ONE owner-authorization command of release 0.6.0.
#
#   scripts/release/authorize_release.sh --doc FILE --pubkey FILE [--dry-run]
#
# FILE   the rendered RELEASE_OWNER_APPROVAL_0.6.0.md the owner reviewed (scripts/release/approval.py render ...)
# PUBKEY the PUBLIC half of the owner's legal-decision key (NOT the release key; the gate refuses that one). The private key stays in the
#        owner's ssh-agent: this script never opens a private key file (the public key is copied to a private directory first, so ssh-keygen
#        cannot fall back to a private key that sits beside it).
#
# What it does, in order (stops at the first failure; nothing is pushed, uploaded or signed except the one document):
#   1. refuses unless the worktree is clean and HEAD is the commit the document names (base-commit);
#   2. verifies the document against that commit (header, manifest hash, owner-only decision set, byte-identical render);
#   3. shows the sha256 of the document and the key fingerprint, and asks the owner to TYPE the digest and the fingerprint;
#   4. signs the document once (ssh-keygen -Y sign, namespace redevops-legal-decision@v1, key from the agent);
#   5. writes the document, its signature, the signer list and the gate entries of the bundled decisions, then asks the gate itself to verify them;
#   6. commits exactly those four paths as the release commit R (one commit; the owner's own git configuration applies).
# --dry-run performs 1, 2 and the checks of 3 to 5 that need no signature, prints what would be written, and changes nothing.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"; ROOT="$(cd "$HERE/../.." && pwd -P)"
DOC="" PUB="" DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --doc) DOC="${2:?}"; shift 2 ;;
    --pubkey) PUB="${2:?}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$DOC" ] && [ -n "$PUB" ] || { echo "usage: authorize_release.sh --doc FILE --pubkey FILE [--dry-run]" >&2; exit 2; }
[ -f "$DOC" ] && [ -f "$PUB" ] || { echo "file not found" >&2; exit 2; }
DOC="$(cd "$(dirname "$DOC")" && pwd -P)/$(basename "$DOC")"; PUB="$(cd "$(dirname "$PUB")" && pwd -P)/$(basename "$PUB")"
PY=(env -i PATH=/usr/bin:/bin:/opt/homebrew/bin LC_ALL=C GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null python3 -I "$HERE/approval.py")
fail() { echo "STOP: $*" >&2; exit 1; }

cd "$ROOT"
[ -z "$(git status --porcelain)" ] || fail "the worktree is not clean"
BASE="$(sed -n 's/^base-commit: //p' "$DOC" | head -1)"
[ "$(git rev-parse HEAD)" = "$BASE" ] || fail "HEAD $(git rev-parse HEAD) is not the commit the document names ($BASE)"
"${PY[@]}" verify --doc "$DOC" --commit "$BASE" || fail "the document does not match its commit"
DIGEST="$("${PY[@]}" digest --doc "$DOC")"
FPR="$(/usr/bin/ssh-keygen -l -f "$PUB" | awk '{print $2}')"
[ -n "$FPR" ] || fail "cannot read the public key"
[ "$FPR" != "SHA256:wUoE8wxKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA" ] || fail "that is the published release key; use a separate legal-decision key"
grep -q "PRIVATE" "$PUB" && fail "that file looks like a private key"
IDS="$(sed -n 's/^decision: \([^ ]*\) .*/\1/p' "$DOC" | tr '\n' ' ')"
echo "document : $DOC"
echo "sha256   : $DIGEST"
echo "commit F : $BASE"
echo "decisions: $IDS"
echo "key      : $FPR   ($PUB)"
if [ "$DRY" = 1 ]; then
  echo "DRY RUN: would sign the document with the agent-held key for the fingerprint above, then write:"
  echo "  docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md (+ .sig), packaging/legal-allowed-signers, packaging/distribution-gate.json"
  echo "  and commit them as the release commit. Nothing was changed."
  exit 0
fi
[ -t 0 ] || [ -r /dev/tty ] || fail "needs a terminal: the owner must type the digest and the fingerprint"
read -r -p "Type the full sha256 of the document to approve THIS document: " T1 < /dev/tty
[ "$T1" = "$DIGEST" ] || fail "the typed digest differs from the document's digest"
read -r -p "Type the key fingerprint (SHA256:...) of your legal-decision key (confirm it against your independent record): " T2 < /dev/tty
[ "$T2" = "$FPR" ] || fail "the typed fingerprint differs from the key file's"
read -r -p "Name that signs (as on your earlier form) [Alex Rybachek]: " NAME < /dev/tty
NAME="${NAME:-Alex Rybachek}"
[ -n "${SSH_AUTH_SOCK:-}" ] || fail "no ssh-agent (SSH_AUTH_SOCK is empty): the private key must be in your agent"
/usr/bin/ssh-add -L 2>/dev/null | grep -qF "$(awk '{print $2}' "$PUB")" || fail "your agent does not hold that key"
TMP="$(mktemp -d)"; trap 'rm -f "$TMP/key.pub" "$TMP/doc" "$TMP/doc.sig"; rmdir "$TMP" 2>/dev/null || true' EXIT
cp "$PUB" "$TMP/key.pub"; cp "$DOC" "$TMP/doc"
[ "$(shasum -a 256 "$TMP/doc" | awk '{print $1}')" = "$DIGEST" ] || fail "the document changed while being copied"
env -i PATH=/usr/bin:/bin SSH_AUTH_SOCK="$SSH_AUTH_SOCK" /usr/bin/ssh-keygen -Y sign -f "$TMP/key.pub" -n redevops-legal-decision@v1 "$TMP/doc" || fail "signing failed"
DATE="$(date -u +%F)"
"${PY[@]}" apply --doc "$DOC" --pubkey "$PUB" --decided-by "$NAME (owner), consolidated approval $DIGEST" --date "$DATE"
cp "$TMP/doc.sig" "$ROOT/docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md.sig"
if ! "${PY[@]}" check; then
  git checkout -- packaging/distribution-gate.json packaging/legal-allowed-signers 2>/dev/null || true
  rm -f docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md.sig
  fail "the gate does not accept the signature; everything was rolled back"
fi
git add docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md.sig packaging/legal-allowed-signers packaging/distribution-gate.json
git commit -q -m "release 0.6.0: consolidated owner approval $DIGEST (signed $DATE by $NAME, key $FPR)

Owner-only decisions: $IDS
Base commit F: $BASE
Counsel decisions are NOT covered and stay OPEN."
echo "RELEASE COMMIT R: $(git rev-parse HEAD)"
echo "Next: scripts/release/release_orchestrate.sh --dry-run   (nothing was pushed)"
