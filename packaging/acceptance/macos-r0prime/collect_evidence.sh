#!/bin/bash
# collect_evidence.sh: bundle what a test session produced into ONE evidence directory with hashes.
#
#   collect_evidence.sh --out DIR [--note TEXT] [--tester NAME] [--dmg FILE] [--installed-app "/Applications/Agentic Apps R0-prime.app"]
#                       [--path FILE_OR_DIR]...
#
# Writes DIR/evidence-transcript.txt (host name, OS build, hardware, user/uid, admin or standard, Gatekeeper and SIP
# state, Command Line Tools / Rosetta presence, time, the dmg's extended attributes (quarantine value), the installed app's
# binary hashes), copies every --path (screenshots, earlier r0p-out directories, notes) into DIR/collected/ keeping the
# relative names (copies, the originals are never modified), and finally writes DIR/SHA256SUMS.txt: the SHA-256 of the
# transcript and of EVERY collected file, in `shasum -a 256 -c` format. Record the printed SHA-256 of SHA256SUMS.txt
# somewhere outside this directory (message, ticket): that one value pins the whole bundle.
# Read-only on the system: no sudo, no keychain (the security tool is never called; whether the login keychain is empty is a
# manual precondition this script cannot and does not try to verify), no network, no GUI.
# Exit status: 0 success, 2 usage error.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"

OUT=""; NOTE=""; TESTER=""; DMG=""; INSTALLED="/Applications/$APP_NAME"; PATHS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="${2:?}"; shift 2;;
    --note) NOTE="${2:?}"; shift 2;;
    --tester) TESTER="${2:?}"; shift 2;;
    --dmg) DMG="${2:?}"; shift 2;;
    --installed-app) INSTALLED="${2:?}"; shift 2;;
    --path) PATHS="${PATHS}${2:?}
"; shift 2;;
    -h|--help) sed -n '2,/^set -u/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown argument: $1";;
  esac
done
[ -n "$OUT" ] || die "--out DIR is required"
[ "$(uname -s)" = Darwin ] || die "macOS only"
[ "$(id -u)" -ne 0 ] || die "do not run as root"
if [ -e "$OUT" ]; then [ -d "$OUT" ] && [ -z "$(ls -A "$OUT")" ] || die "--out exists and is not empty: $OUT"; fi
mkdir -p "$OUT/collected" || die "cannot create $OUT"
OUT="$(abs_path "$OUT")"
TRANSCRIPT="$OUT/evidence-transcript.txt"; : >"$TRANSCRIPT"

header "collect_evidence"
L "tester: ${TESTER:-<not given>}"
L "note: ${NOTE:-<none>}"
L ""
L "=== host"
L "ComputerName: $(scutil --get ComputerName 2>/dev/null || echo '?')   hostname: $(hostname)"
L "OS: $(sw_vers -productName) $(sw_vers -productVersion) build $(sw_vers -buildVersion)   kernel: $(uname -srm)"
L "hardware model: $(sysctl -n hw.model 2>/dev/null)   cpu: $(sysctl -n machdep.cpu.brand_string 2>/dev/null)   arch: $(uname -m)"
L "user: $(id -un) uid=$(id -u) gid=$(id -g) groups: $(id -Gn)"
if dsmemberutil checkmembership -U "$(id -un)" -G admin 2>/dev/null | grep -q 'is a member'; then L "account type: ADMINISTRATOR (the kit expects a STANDARD user)"; else L "account type: standard (not in the admin group)"; fi
L "HOME: $HOME   TMPDIR: ${TMPDIR:-<unset>}"
L "console user: $(stat -f %Su /dev/console 2>/dev/null)   (the account that owns the screen; must equal the test user for GUI steps)"
L "Gatekeeper: $(spctl --status 2>&1)"
L "SIP: $(csrutil status 2>&1 | head -1)"
if xcode-select -p >/dev/null 2>&1; then L "developer tools: PRESENT ($(xcode-select -p)); a clean acceptance account should have NONE"; else L "developer tools: none selected (xcode-select -p fails), as expected for a clean account"; fi
[ -e /Library/Apple/usr/share/rosetta/rosetta ] && L "Rosetta 2: installed (irrelevant for this arm64-only candidate; see INTEL-ROSETTA-TEST-PLAN.md)" || L "Rosetta 2: not installed"
L "free disk on /: $(df -h / | awk 'NR==2{print $4}')   uptime: $(uptime | sed 's/^ *//')"
L "network: default route interface: $(route -n get default 2>/dev/null | awk '/interface:/{print $2}')  (record separately whether it was offline for the offline-launch step)"
L ""
L "=== dmg"
if [ -n "$DMG" ]; then
  if [ -f "$DMG" ]; then
    L "dmg: $(abs_path "$DMG")  size $(stat -f %z "$DMG")  sha256 $(sha256_of "$DMG")"
    L "extended attributes (quarantine value, if any, appears here):"; xattr -l "$DMG" 2>&1 | sed 's/^/    /' | tee_t
  else L "dmg NOT FOUND: $DMG"; fi
else L "(no --dmg given)"; fi
L ""
L "=== installed app: $INSTALLED"
if [ -d "$INSTALLED" ]; then
  L "present; Info.plist: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$INSTALLED/Contents/Info.plist" 2>&1) $(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$INSTALLED/Contents/Info.plist" 2>&1)"
  for b in "$INSTALLED"/Contents/MacOS/*; do [ -f "$b" ] && L "    $(basename "$b") sha256 $(sha256_of "$b")"; done
  L "extended attributes of the bundle directory:"; xattr -l "$INSTALLED" 2>&1 | sed 's/^/    /' | tee_t
  L "codesign (summary):"; codesign -dv "$INSTALLED" 2>&1 | grep -E '^(Identifier|Authority|TeamIdentifier|Signature|CodeDirectory)' | sed 's/^/    /' | tee_t
else L "not installed (or not at that path)"; fi

# ---------------------------------------------------------------- collect (copies)
L ""; L "=== collected files (copies; originals untouched)"
NCOPIED=0
copy_one() { # source-file dest-relative
  local src="$1" rel="$2" dst
  dst="$OUT/collected/$rel"
  mkdir -p "$(dirname "$dst")"
  if [ -e "$dst" ]; then rel="${rel}.dup$NCOPIED"; dst="$OUT/collected/$rel"; fi
  cp -p "$src" "$dst" && { NCOPIED=$((NCOPIED + 1)); L "    $rel  ($(stat -f %z "$dst") bytes, source mtime $(stat -f %Sm -t %Y-%m-%dT%H:%M:%S "$src"))"; }
}
while IFS= read -r pth; do
  [ -n "$pth" ] || continue
  if [ -f "$pth" ]; then copy_one "$pth" "$(basename "$pth")"
  elif [ -d "$pth" ]; then
    base="$(basename "$pth")"
    while IFS= read -r f; do copy_one "$f" "$base/${f#$pth/}"; done <<FILES
$(find "$pth" -type f -not -name '.DS_Store' | sort)
FILES
  else L "    NOT FOUND, skipped: $pth"; fi
done <<PATHLIST
$PATHS
PATHLIST
L "$NCOPIED file(s) copied"

# ---------------------------------------------------------------- hashes (transcript last, then the manifest hash)
L ""; L "=== hashes"
L "SHA256SUMS.txt (sha256 of the transcript and every collected file) is written next; the transcript's own hash cannot be inside the transcript."
( cd "$OUT" && { shasum -a 256 evidence-transcript.txt; find collected -type f | sort | while IFS= read -r f; do shasum -a 256 "$f"; done; } >SHA256SUMS.txt )
cat "$OUT/SHA256SUMS.txt" | sed 's/^/    /'
( cd "$OUT" && shasum -a 256 -c SHA256SUMS.txt >/dev/null 2>&1 ) && echo "verified: shasum -a 256 -c SHA256SUMS.txt passes" || echo "WARNING: self-check of SHA256SUMS.txt failed"
echo "record this out of band: SHA-256 of SHA256SUMS.txt = $(sha256_of "$OUT/SHA256SUMS.txt")"
echo "evidence directory: $OUT"
