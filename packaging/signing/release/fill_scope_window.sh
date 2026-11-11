#!/bin/bash
# fill_scope_window.sh <scope-draft> [<out-file>]
# Writes a COPY of the scope draft with not_before=<now UTC> and not_after=<now + 23 h 59 m UTC> and prints the sha256 of the copy to STDOUT (everything else goes to
# stderr): that value is what you pass to --i-have-owner-authorization. The procedure's maximum window is 24 h; a 24 h 00 m window failed pre-run verification in the
# previous pass, hence 23 h 59 m. Run it LAST, right before the pre-run verification and the real run: the window starts now and must also cover the notary wait (up to 30 min + 90 s).
# Default <out-file>: the draft's directory and name without ".DRAFT" (scope-macos.DRAFT.txt -> scope-macos.txt, scope-sshsig.DRAFT.txt -> scope-sshsig.txt).
# Refuses to overwrite an existing out-file. For the macOS scope it refuses a draft that still has identity/profile/Team ID placeholders (run tools/fill_scope_identity.sh
# first; --allow-identity-placeholders is for tests only). No secret, no keychain, no network.
set -u
PATH=/usr/bin:/bin
ALLOW=0; ARGS=()
for a in "$@"; do case "$a" in --allow-identity-placeholders) ALLOW=1;; *) ARGS+=("$a");; esac; done
IN="${ARGS[0]:?usage: fill_scope_window.sh <scope-draft> [<out-file>]}"
if [ "${#ARGS[@]}" -ge 2 ]; then OUT="${ARGS[1]}"; else
  d="$(cd "$(dirname "$IN")" && pwd)"; b="$(basename "$IN")"
  case "$b" in *.DRAFT.txt) OUT="$d/${b%.DRAFT.txt}.txt";; *) OUT="$d/${b%.txt}.window.txt";; esac
fi
[ -f "$IN" ] && [ ! -L "$IN" ] || { echo "ERROR: $IN is not a regular file" >&2; exit 2; }
[ ! -e "$OUT" ] || { echo "ERROR: $OUT exists; refusing to overwrite (remove it by name first)" >&2; exit 2; }
grep -q '^not_before=<<' "$IN" && grep -q '^not_after=<<' "$IN" || { echo "ERROR: the input has no not_before/not_after placeholders" >&2; exit 2; }
if grep -q '^\(identity_sha256\|notary_profile_sha256\|team_id\)=<<' "$IN" && [ "$ALLOW" = 0 ]; then
  echo "ERROR: identity/profile/Team ID are still placeholders: run tools/fill_scope_identity.sh first" >&2; exit 2; fi
NB="$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"; NA="$(/bin/date -u -v+23H -v+59M +%Y-%m-%dT%H:%M:%SZ)"
umask 077
/usr/bin/awk -v nb="$NB" -v na="$NA" '/^not_before=<</ {print "not_before=" nb; next} /^not_after=<</ {print "not_after=" na; next} {print}' "$IN" > "$OUT" || { echo "ERROR: cannot write $OUT" >&2; exit 2; }
echo "not_before=$NB" >&2; echo "not_after=$NA" >&2; echo "wrote $OUT" >&2
echo "scope sha256 (for --i-have-owner-authorization):" >&2
/usr/bin/shasum -a 256 "$OUT" | /usr/bin/cut -d' ' -f1
