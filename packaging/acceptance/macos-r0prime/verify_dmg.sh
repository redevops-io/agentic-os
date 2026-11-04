#!/bin/bash
# verify_dmg.sh: read-only verification of the Agentic Apps macOS dmg. The same script is used for the unsigned local
# development build (--expect unsigned, the default) and for a signed + notarized + stapled candidate (--expect signed).
#
#   verify_dmg.sh [--expect unsigned|signed] [--team-id ID] [--expect-version V] [--expect-app-stapled]
#                 [--private-copy] [--out DIR] <dmg> <expected-sha256-file>
#
# <expected-sha256-file>: `shasum -a 256` format ("<64 hex>  <file name>"), produced on the BUILD machine. The dmg is
# refused (nothing is attached) unless its file name is listed and the hash matches.
# Never modifies the dmg (hash and extended attributes are compared before and after), never signs, never runs the app,
# never touches a keychain, needs no sudo. Everything it writes goes under --out (default ./r0p-out/verify-<UTC>).
# Needs the Xcode Command Line Tools for vtool and stapler; without them those rows are SKIPPED (not passed).
# Exit status: 0 no FAIL, 1 at least one FAIL, 2 usage/precondition error.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_lib.sh
. "$HERE/_lib.sh"

EXPECT="unsigned"; TEAM_ID=""; EXPECT_VERSION=""; EXPECT_APP_STAPLED=0; PRIVATE_COPY=0; OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --expect) EXPECT="${2:?}"; shift 2;;
    --team-id) TEAM_ID="${2:?}"; shift 2;;
    --expect-version) EXPECT_VERSION="${2:?}"; shift 2;;
    --expect-app-stapled) EXPECT_APP_STAPLED=1; shift;;
    --private-copy) PRIVATE_COPY=1; shift;;
    --out) OUT="${2:?}"; shift 2;;
    -h|--help) sed -n '2,/^set -u/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0;;
    --) shift; break;;
    -*) die "unknown option: $1";;
    *) break;;
  esac
done
[ $# -eq 2 ] || die "usage: verify_dmg.sh [options] <dmg> <expected-sha256-file>   (see --help)"
DMG_IN="$1"; SHAFILE="$2"
case "$EXPECT" in unsigned|signed) ;; *) die "--expect must be unsigned or signed";; esac
[ "$(uname -s)" = Darwin ] || die "macOS only"
[ "$(id -u)" -ne 0 ] || die "do not run as root"
[ -f "$DMG_IN" ] || die "dmg not found: $DMG_IN"
[ -f "$SHAFILE" ] || die "expected-sha256 file not found: $SHAFILE"
DMG="$(abs_path "$DMG_IN")"

[ -n "$OUT" ] || OUT="$PWD/r0p-out/verify-$(utc_stamp)"
if [ -e "$OUT" ]; then [ -d "$OUT" ] && [ -z "$(ls -A "$OUT")" ] || die "--out exists and is not empty: $OUT"; fi
mkdir -p "$OUT/mnt" "$OUT/work" || die "cannot create $OUT"
OUT="$(abs_path "$OUT")"
TRANSCRIPT="$OUT/verify-transcript.txt"; : >"$TRANSCRIPT"

MNT="$OUT/mnt"; ATTACHED=0; WORKDMG=""
cleanup() {
  local rc=$? i
  set +e
  if [ "$ATTACHED" = 1 ]; then
    for i in 1 2 3; do hdiutil detach "$MNT" -quiet 2>/dev/null && { ATTACHED=0; break; }; sleep 1; done
    # last resort for OUR OWN mount point only
    [ "$ATTACHED" = 1 ] && hdiutil detach "$MNT" -force -quiet 2>/dev/null && ATTACHED=0
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

header "verify_dmg ($EXPECT)"
L "dmg: $DMG"
L "expected-sha256 file: $SHAFILE"
L "mode: --expect $EXPECT${TEAM_ID:+ --team-id $TEAM_ID}${EXPECT_VERSION:+ --expect-version $EXPECT_VERSION}"
L "NOTE: spctl/stapler results below are ASSESSMENTS of the files, not the Finder first-launch path (see MANUAL-CHECKLIST.md)."
L ""

# ---------------------------------------------------------------- 1. hash against the expected file (fail closed)
NAME="$(basename "$DMG")"
EXP="$(awk -v n="$NAME" '{h=substr($0,1,64); f=substr($0,65); sub(/^ [ *]/,"",f); if (h ~ /^[0-9a-fA-F]{64}$/ && f==n) {print tolower(h); exit}}' "$SHAFILE")"
ACT="$(sha256_of "$DMG")"
BEFORE_X="$(xattr -l "$DMG" 2>&1)"
case "$BEFORE_X" in *com.apple.diskimages.recentcksum*) PRE_CKSUM=1;; *) PRE_CKSUM=0;; esac
BEFORE_STAT="$(stat -f '%z %m' "$DMG")"
L "dmg size/mtime: $BEFORE_STAT"
if [ -z "$EXP" ]; then
  rec FAIL H-01 "file name '$NAME' is not listed (with a valid 64-hex hash) in $SHAFILE; refusing to attach"
  summary; exit 1
fi
if [ "$ACT" = "$EXP" ]; then rec PASS H-01 "sha256 $ACT matches the expected file"; else
  rec FAIL H-01 "sha256 MISMATCH: actual $ACT expected $EXP; refusing to attach"; summary; exit 1; fi

# quarantine / provenance attributes: recorded, never changed
Q="$(xattr -p com.apple.quarantine "$DMG" 2>/dev/null || true)"
if [ -n "$Q" ]; then rec INFO H-02 "com.apple.quarantine present on the dmg: '$Q' (first field = flags; 0081 is the value a browser writes on a fresh download; the meaning of other values is INFERRED, see README)"
else rec INFO H-02 "no com.apple.quarantine on the dmg (a copy made without it will NOT exercise the Gatekeeper first-launch path)"; fi
L "extended attributes of the dmg (before):"; printf '%s\n' "${BEFORE_X:-<none>}" | sed 's/^/    /' | tee_t

# ---------------------------------------------------------------- 2. attach read-only on our own mount point
SRC="$DMG"
if [ "$PRIVATE_COPY" = 1 ]; then
  WORKDMG="$OUT/work/copy.dmg"; cp -p "$DMG" "$WORKDMG" || die "cannot copy the dmg"
  [ "$(sha256_of "$WORKDMG")" = "$ACT" ] || die "private copy differs from the verified dmg"
  SRC="$WORKDMG"; L "attaching a private copy ($WORKDMG)"
fi
# -noverify: the sha256 above already proves the bytes; with verification enabled hdiutil WRITES a
# com.apple.diskimages.recentcksum extended attribute onto the dmg (observed on macOS 27), which this script must not do.
cap "hdiutil attach -readonly -nobrowse -noverify -mountpoint $MNT" hdiutil attach -readonly -nobrowse -noautoopen -noverify -mountpoint "$MNT" "$SRC"
if [ "$CAP_RC" -ne 0 ]; then
  rec FAIL M-01 "hdiutil attach failed (if 'Resource busy': the dmg is mounted elsewhere; detach it or use --private-copy)"
  summary; exit 1
fi
ATTACHED=1
rec PASS M-01 "attached read-only at $MNT"

L ""; L "--- top level of the volume"
ls -laO "$MNT" 2>&1 | sed 's/^/    /' | tee_t
L "--- tree (depth 4, names only)"
( cd "$MNT" && find . -maxdepth 4 -not -path './.fseventsd*' -not -path './.Trashes*' | sort ) 2>&1 | sed 's/^/    /' | tee_t

APPS=""; NAPP=0
for a in "$MNT"/*.app; do [ -d "$a" ] && { APPS="$a"; NAPP=$((NAPP + 1)); }; done
if [ "$NAPP" -ne 1 ]; then rec FAIL C-01 "expected exactly one .app in the dmg root, found $NAPP"; summary; exit 1; fi
APP="$APPS"; rec PASS C-01 "one app bundle: $(basename "$APP")"
if [ -L "$MNT/Applications" ] && [ "$(readlink "$MNT/Applications")" = "/Applications" ]; then rec PASS C-02 "Applications -> /Applications symlink present"
else rec WARN C-02 "no Applications -> /Applications symlink at the dmg root (drag-to-install hint missing)"; fi

# ---------------------------------------------------------------- 3. licences and notices
LIC="$APP/Contents/Resources/Licenses"
if [ -d "$LIC" ]; then rec PASS L-01 "Contents/Resources/Licenses/ present"; else rec FAIL L-01 "Contents/Resources/Licenses/ missing"; fi
for f in THIRD_PARTY_NOTICES.txt LICENSE LICENSE-COMMONS-CLAUSE LICENSE.md LICENSING.md LICENSES-MANIFEST.json NOTICES_MISSING.md; do
  if [ -s "$LIC/$f" ]; then rec PASS "L-02:$f" "present, $(stat -f %z "$LIC/$f") bytes"; else rec FAIL "L-02:$f" "missing or empty in Licenses/"; fi
done
# every file listed in LICENSES-MANIFEST.json must have the listed sha256 (awk over the pretty-printed JSON)
if [ -s "$LIC/LICENSES-MANIFEST.json" ]; then
  MBAD=0; MN=0
  while IFS='	' read -r mname msha; do
    [ -n "$mname" ] || continue; MN=$((MN + 1))
    if [ ! -f "$LIC/$mname" ] || [ "$(sha256_of "$LIC/$mname")" != "$msha" ]; then MBAD=$((MBAD + 1)); L "    manifest mismatch: $mname"; fi
  done <<MANIFEST
$(awk '/^    "[^"]+": *\{/ {n=$1; gsub(/[":{ ]/,"",n); gsub(/:$/,"",n)} /"sha256"/ {s=$2; gsub(/[",]/,"",s); if (n!="") printf "%s\t%s\n", n, s; n=""}' "$LIC/LICENSES-MANIFEST.json")
MANIFEST
  if [ "$MN" -eq 0 ]; then rec WARN L-03 "could not read any entry from LICENSES-MANIFEST.json"
  elif [ "$MBAD" -eq 0 ]; then rec PASS L-03 "all $MN files listed in LICENSES-MANIFEST.json match their sha256"
  else rec FAIL L-03 "$MBAD of $MN manifest entries do not match"; fi
fi
if [ -s "$LIC/NOTICES_MISSING.md" ]; then rec INFO L-04 "NOTICES_MISSING.md is shipped ($(wc -l <"$LIC/NOTICES_MISSING.md" | tr -d ' ') lines): components without a licence text are listed there; read it, presence is not a clearance"; fi
if [ -e "$MNT/Licenses" ] || [ -e "$MNT/THIRD_PARTY_NOTICES.txt" ]; then rec INFO L-05 "licence material also at the dmg root"; else rec INFO L-05 "no Licenses/ at the dmg root (they are inside the app bundle only)"; fi

# ---------------------------------------------------------------- 4. Info.plist and Mach-O inventory
PL="$APP/Contents/Info.plist"
pb() { /usr/libexec/PlistBuddy -c "Print :$1" "$PL" 2>/dev/null; }
BID="$(pb CFBundleIdentifier)"; BEXE="$(pb CFBundleExecutable)"; BVER="$(pb CFBundleVersion)"; BSVER="$(pb CFBundleShortVersionString)"; LSMIN="$(pb LSMinimumSystemVersion)"
L "Info.plist: CFBundleIdentifier=$BID CFBundleExecutable=$BEXE CFBundleVersion=$BVER CFBundleShortVersionString=$BSVER LSMinimumSystemVersion=$LSMIN"
[ "$BID" = "$APP_ID" ] && rec PASS P-01 "CFBundleIdentifier = $APP_ID" || rec FAIL P-01 "CFBundleIdentifier is '$BID', expected $APP_ID"
[ -n "$BVER" ] && rec INFO P-02 "CFBundleVersion=$BVER CFBundleShortVersionString=$BSVER" || rec FAIL P-02 "CFBundleVersion missing"
if [ -n "$EXPECT_VERSION" ]; then [ "$BVER" = "$EXPECT_VERSION" ] && rec PASS P-03 "CFBundleVersion equals the expected $EXPECT_VERSION" || rec FAIL P-03 "CFBundleVersion '$BVER' != expected '$EXPECT_VERSION'"; fi
[ -n "$LSMIN" ] || rec FAIL P-04 "LSMinimumSystemVersion missing"
[ -x "$APP/Contents/MacOS/$BEXE" ] && rec PASS P-05 "CFBundleExecutable '$BEXE' exists and is executable" || rec FAIL P-05 "CFBundleExecutable '$BEXE' missing"

MACHOS=""; NM=0
while IFS= read -r f; do
  [ -f "$f" ] || continue
  case "$(file -b "$f" 2>/dev/null)" in Mach-O*) MACHOS="${MACHOS}${f}
"; NM=$((NM + 1));; esac
done <<LIST
$(find "$APP" -type f 2>/dev/null)
LIST
L "Mach-O files in the bundle: $NM"
MAXMINOS="0"
CLT=0; have_clt && CLT=1
[ "$CLT" = 1 ] || rec SKIP T-00 "Command Line Tools not installed: vtool and stapler rows are skipped (run this script on a build/verification account that has them)"
while IFS= read -r f; do
  [ -n "$f" ] || continue
  rel="${f#$APP/}"; desc="$(file -b "$f")"
  L "  $rel: sha256=$(sha256_of "$f") $desc"
  case "$desc" in
    *arm64*) case "$desc" in *x86_64*) rec WARN A-01 "$rel also contains x86_64 (universal); this kit's expectation is arm64-only";; *) rec PASS A-01 "$rel: arm64 only (Intel is NOT supported by this candidate; see INTEL-ROSETTA-TEST-PLAN.md)";; esac;;
    *) rec FAIL A-01 "$rel: not arm64 ($desc)";;
  esac
  if [ "$CLT" = 1 ]; then
    mo="$(vtool -show-build "$f" 2>/dev/null | awk '/minos/{print $2; exit}')"
    if [ -n "$mo" ]; then L "      vtool minos=$mo"; vge "$mo" "$MAXMINOS" && MAXMINOS="$mo"; else rec WARN T-01 "$rel: vtool -show-build gave no minos"; fi
  fi
done <<LIST
$MACHOS
LIST
if [ "$CLT" = 1 ] && [ "$MAXMINOS" != "0" ] && [ -n "$LSMIN" ]; then
  if vge "$LSMIN" "$MAXMINOS"; then rec PASS P-06 "LSMinimumSystemVersion $LSMIN >= highest Mach-O minos $MAXMINOS"
  else rec WARN P-06 "LSMinimumSystemVersion $LSMIN is LOWER than the highest Mach-O minos $MAXMINOS: the plist claims macOS versions on which the binaries cannot load (known v0.3.2 finding, owner decision)"; fi
fi

# ---------------------------------------------------------------- 5. code signature
SIGNED_RE='^Authority=Developer ID Application:'
dv() { codesign -dvvv "$1" 2>&1; }
L ""; L "=== codesign details"
TEAMS=""
SIDECAR_REL="Contents/MacOS/redevops-sidecar"
while IFS= read -r f; do
  [ -n "$f" ] || continue
  rel="${f#$APP/}"; D="$(dv "$f")"
  L "--- codesign -dvvv $rel"; printf '%s\n' "$D" | sed 's/^/    /' | tee_t
  sig="$(printf '%s\n' "$D" | sed -n 's/^Signature=//p' | head -1)"
  team="$(printf '%s\n' "$D" | sed -n 's/^TeamIdentifier=//p' | head -1)"
  flags="$(printf '%s\n' "$D" | sed -n 's/.*flags=\(0x[0-9a-f]*\)(\([^)]*\)).*/\1(\2)/p' | head -1)"
  if [ "$EXPECT" = signed ]; then
    printf '%s\n' "$D" | grep -q "$SIGNED_RE" && rec PASS S-01 "$rel: Developer ID Application authority" || rec FAIL S-01 "$rel: no Developer ID Application authority"
    printf '%s\n' "$flags" | grep -q runtime && rec PASS S-02 "$rel: hardened runtime flag ($flags)" || rec FAIL S-02 "$rel: hardened runtime flag missing ($flags)"
    printf '%s\n' "$D" | grep -q '^Timestamp=' && rec PASS S-03 "$rel: secure timestamp present" || rec FAIL S-03 "$rel: no secure timestamp"
    if [ -n "$team" ] && [ "$team" != "not set" ]; then
      rec PASS S-04 "$rel: TeamIdentifier=$team"; TEAMS="${TEAMS}${team}
"
      [ -z "$TEAM_ID" ] || { [ "$team" = "$TEAM_ID" ] && rec PASS S-05 "$rel: Team ID equals --team-id" || rec FAIL S-05 "$rel: Team ID '$team' != --team-id '$TEAM_ID'"; }
    else rec FAIL S-04 "$rel: no TeamIdentifier"; fi
  else
    if printf '%s\n' "$D" | grep -q "$SIGNED_RE"; then rec FAIL S-01 "$rel: carries a Developer ID signature but --expect unsigned was given (use --expect signed)"
    else rec PASS S-01 "$rel: no Developer ID signature (signature='${sig:-none}', as expected for an unsigned local build)"; fi
  fi
done <<LIST
$MACHOS
LIST
if [ "$EXPECT" = signed ]; then
  NT="$(printf '%s' "$TEAMS" | sort -u | grep -c .)"
  [ "$NT" -le 1 ] && rec PASS S-06 "one Team ID across all Mach-O files" || rec FAIL S-06 "more than one Team ID across Mach-O files"
fi

cap "codesign --verify --deep --strict --verbose=2 (app)" codesign --verify --deep --strict --verbose=2 "$APP"
if [ "$EXPECT" = signed ]; then [ "$CAP_RC" -eq 0 ] && rec PASS S-10 "app verifies (deep, strict)" || rec FAIL S-10 "codesign verify failed for the app"
else L "    (unsigned mode: verify result recorded, not asserted: exit $CAP_RC)"; rec INFO S-10 "codesign --verify --deep --strict on the unsigned app exited $CAP_RC"; fi
D="$(dv "$APP")"
sealed="$(printf '%s\n' "$D" | grep '^Sealed Resources' | head -1)"
nlic=0; [ -d "$LIC" ] && nlic="$(find "$LIC" -type f | wc -l | tr -d ' ')"
sfiles="$(printf '%s\n' "$sealed" | sed -n 's/.*files=\([0-9]*\).*/\1/p')"
if [ "$EXPECT" = signed ]; then
  case "$sealed" in
    ""|*=none*) rec FAIL S-11 "bundle has no sealed resources (Licenses/ and Info.plist are not covered)";;
    *) if [ -n "$sfiles" ] && [ "$sfiles" -ge "$nlic" ]; then rec PASS S-11 "bundle seals its resources ($sealed; Licenses/ has $nlic files)"
       else rec FAIL S-11 "sealed resources ($sealed) cannot cover the $nlic files of Licenses/"; fi;;
  esac
else rec INFO S-11 "bundle '${sealed:-Sealed Resources=none}'; a signed candidate must seal the $nlic Licenses/ files"; fi

cap "codesign verify (dmg)" codesign --verify --strict --verbose=2 "$DMG"
DD="$(dv "$DMG")"; L "--- codesign -dvvv (dmg)"; printf '%s\n' "$DD" | sed 's/^/    /' | tee_t
if [ "$EXPECT" = signed ]; then
  [ "$CAP_RC" -eq 0 ] && rec PASS S-20 "dmg signature verifies" || rec FAIL S-20 "dmg signature does not verify"
  printf '%s\n' "$DD" | grep -q "$SIGNED_RE" && rec PASS S-21 "dmg: Developer ID Application authority" || rec FAIL S-21 "dmg: no Developer ID Application authority"
  dteam="$(printf '%s\n' "$DD" | sed -n 's/^TeamIdentifier=//p' | head -1)"
  [ -n "$dteam" ] && [ "$dteam" != "not set" ] && rec PASS S-22 "dmg TeamIdentifier=$dteam" || rec FAIL S-22 "dmg has no TeamIdentifier"
else
  [ "$CAP_RC" -ne 0 ] && rec PASS S-20 "dmg is not signed (as expected for an unsigned local build)" || rec FAIL S-20 "dmg verifies as signed but --expect unsigned was given"
fi

# ---------------------------------------------------------------- 6. entitlements per Mach-O (fail closed)
L ""; L "=== entitlements"
ENT_JSON=""; ENT_WHY=""
read_ent() { # file -> ENT_JSON ("{}" only when codesign positively reports none) or ENT_WHY
  local f="$1" x="$OUT/work/ent.xml" e="$OUT/work/ent.err" h="$OUT/work/ent.txt" rc=0
  ENT_JSON=""; ENT_WHY=""
  codesign -d --entitlements - --xml "$f" >"$x" 2>"$e" || rc=$?
  if [ "$rc" -ne 0 ]; then rc=0; codesign -d --entitlements :- "$f" >"$x" 2>"$e" || rc=$?; fi
  [ "$rc" -eq 0 ] || { ENT_WHY="codesign -d --entitlements failed ($rc)"; return 1; }
  grep -q '^Executable=' "$e" || { ENT_WHY="codesign produced no Executable= report"; return 1; }
  if [ -s "$x" ]; then
    ENT_JSON="$(plutil -convert json -o - "$x" 2>/dev/null)" || { ENT_WHY="entitlement data is not a plist"; return 1; }
    [ -n "$ENT_JSON" ] || { ENT_WHY="entitlement data parsed to nothing"; return 1; }
  else
    codesign -d --entitlements - "$f" >"$h" 2>>"$e" || { ENT_WHY="human-readable dump failed"; return 1; }
    [ -s "$h" ] && { ENT_WHY="XML dump empty but human-readable dump is not"; return 1; }
    ENT_JSON="{}"
  fi
  return 0
}
EXPECTED_SIDECAR_ENT='{"com.apple.security.cs.disable-library-validation":true}'
while IFS= read -r f; do
  [ -n "$f" ] || continue
  rel="${f#$APP/}"
  if ! read_ent "$f"; then rec FAIL E-01 "$rel: cannot read entitlements ($ENT_WHY); not assuming none"; continue; fi
  L "  $rel: entitlements = $ENT_JSON"
  case "$ENT_JSON" in *get-task-allow*) rec FAIL E-02 "$rel carries get-task-allow (debug entitlement; notarization would refuse it)"; continue;; esac
  if [ "$rel" = "$SIDECAR_REL" ]; then
    if [ "$EXPECT" = signed ]; then
      [ "$ENT_JSON" = "$EXPECTED_SIDECAR_ENT" ] && rec PASS E-03 "$rel: exactly {disable-library-validation: true}" || rec FAIL E-03 "$rel: entitlements differ from the expected single key: $ENT_JSON"
    else rec INFO E-03 "$rel (unsigned build): entitlements $ENT_JSON; the signed candidate must carry exactly $EXPECTED_SIDECAR_ENT"; fi
  else
    if [ "$EXPECT" = signed ]; then
      [ "$ENT_JSON" = "{}" ] && rec PASS E-04 "$rel: no entitlements" || rec FAIL E-04 "$rel: unexpected entitlements $ENT_JSON"
    else [ "$ENT_JSON" = "{}" ] && rec PASS E-04 "$rel: no entitlements" || rec WARN E-04 "$rel: entitlements $ENT_JSON"; fi
  fi
  case "$rel" in "$SIDECAR_REL"|"Contents/MacOS/$BEXE") ;; *) rec FAIL E-05 "$rel: Mach-O file that is not in the signing plan (sidecar, launcher): add it to the plan before signing";; esac
done <<LIST
$MACHOS
LIST

# ---------------------------------------------------------------- 7. Gatekeeper and stapler (assessments)
L ""; L "=== Gatekeeper assessments"
cap "spctl -a -t exec -vv (app inside the mounted dmg)" spctl -a -t exec -vv "$APP"
SP_APP_RC=$CAP_RC; SP_APP_OUT="$CAP_OUT"
cap "spctl -a -t open --context context:primary-signature -vv (dmg)" spctl -a -t open --context context:primary-signature -vv "$DMG"
SP_DMG_RC=$CAP_RC; SP_DMG_OUT="$CAP_OUT"
if [ "$EXPECT" = signed ]; then
  { [ "$SP_APP_RC" -eq 0 ] && printf '%s' "$SP_APP_OUT" | grep -q 'source=Notarized Developer ID'; } && rec PASS G-01 "spctl accepts the app: source=Notarized Developer ID" || rec FAIL G-01 "spctl did not accept the app as Notarized Developer ID (exit $SP_APP_RC)"
  { [ "$SP_DMG_RC" -eq 0 ] && printf '%s' "$SP_DMG_OUT" | grep -q 'source=Notarized Developer ID'; } && rec PASS G-02 "spctl accepts the dmg: source=Notarized Developer ID" || rec FAIL G-02 "spctl did not accept the dmg as Notarized Developer ID (exit $SP_DMG_RC)"
else
  [ "$SP_APP_RC" -ne 0 ] && rec PASS G-01 "spctl rejects the unsigned app (expected)" || rec FAIL G-01 "spctl ACCEPTED an unsigned app: investigate this host's assessment policy before trusting it"
  [ "$SP_DMG_RC" -ne 0 ] && rec PASS G-02 "spctl rejects the unsigned dmg (expected)" || rec FAIL G-02 "spctl ACCEPTED an unsigned dmg: investigate"
fi
if [ "$CLT" = 1 ]; then
  cap "xcrun stapler validate (dmg)" xcrun stapler validate "$DMG"
  if [ "$EXPECT" = signed ]; then [ "$CAP_RC" -eq 0 ] && rec PASS G-03 "stapler validate: the dmg carries a valid ticket" || rec FAIL G-03 "stapler validate failed for the dmg"
  else [ "$CAP_RC" -ne 0 ] && rec PASS G-03 "dmg has no stapled ticket (expected for an unsigned build)" || rec FAIL G-03 "stapler validated an unsigned dmg: investigate"; fi
  cap "xcrun stapler validate (app inside the dmg)" xcrun stapler validate "$APP"
  if [ "$EXPECT_APP_STAPLED" = 1 ]; then [ "$CAP_RC" -eq 0 ] && rec PASS G-04 "the app carries a valid ticket (--expect-app-stapled)" || rec FAIL G-04 "the app has no stapled ticket but --expect-app-stapled was given"
  else [ "$CAP_RC" -ne 0 ] && rec INFO G-04 "the app has no stapled ticket (expected: only the dmg is stapled; an app copied out relies on the online notarization lookup)" || rec INFO G-04 "the app carries a ticket"; fi
else rec SKIP G-03 "stapler not available (no Command Line Tools)"; fi

# ---------------------------------------------------------------- 8. detach, prove nothing changed
for i in 1 2 3; do hdiutil detach "$MNT" -quiet 2>/dev/null && { ATTACHED=0; break; }; sleep 1; done
[ "$ATTACHED" = 0 ] && rec PASS D-01 "detached" || rec FAIL D-01 "could not detach $MNT (run: hdiutil detach '$MNT')"
mount | grep -qF " on $MNT " && rec FAIL D-02 "mount point still in the mount table" || rec PASS D-02 "no mount of $MNT remains"
AFTER_X="$(xattr -l "$DMG" 2>&1)"
if [ "$PRE_CKSUM" = 0 ]; then case "$AFTER_X" in *com.apple.diskimages.recentcksum*) rec WARN D-04 "attach added a com.apple.diskimages.recentcksum attribute to the dmg (a checksum cache, not content; quarantine unaffected)";; esac; fi
AFTER_X="$(printf '%s\n' "$AFTER_X" | grep -v '^com.apple.diskimages.recentcksum')"; BEFORE_X="$(printf '%s\n' "$BEFORE_X" | grep -v '^com.apple.diskimages.recentcksum')"
AFTER_STAT="$(stat -f '%z %m' "$DMG")"
[ "$(sha256_of "$DMG")" = "$ACT" ] && [ "$AFTER_X" = "$BEFORE_X" ] && [ "$AFTER_STAT" = "$BEFORE_STAT" ] && rec PASS D-03 "dmg bytes, size, mtime and extended attributes unchanged" || rec FAIL D-03 "the dmg changed during verification"
[ -n "$WORKDMG" ] && rm -f "$WORKDMG"
rmdir "$MNT" 2>/dev/null || true

# ---------------------------------------------------------------- expected results table
cat <<'TABLE' | tee_t

Expected results by mode (what each row above asserts)
  row   check                                  --expect unsigned (local dev build)     --expect signed (Developer ID + notarized + stapled dmg)
  S-01  Developer ID authority on every Mach-O absent (Signature=adhoc / linker-signed)  present on launcher and sidecar, one Team ID
  S-02  hardened runtime flag                    not asserted                            flags=0x10000(runtime) on launcher and sidecar
  S-03  secure timestamp                         not asserted                            Timestamp= present
  S-10  codesign --verify --deep --strict app    recorded only                           exit 0
  S-11  sealed resources (Licenses/, Info.plist) recorded only                           sealed (not "none")
  S-20  dmg signature                            absent                                  verifies, Developer ID authority, Team ID
  E-03  sidecar entitlements                     recorded (none expected)                exactly disable-library-validation=true
  E-04  launcher entitlements                    none                                    none
  E-02  get-task-allow                           must not be present                     must not be present
  G-01  spctl -a -t exec on the app              rejected                                accepted, source=Notarized Developer ID
  G-02  spctl on the dmg (primary-signature)     rejected                                accepted, source=Notarized Developer ID
  G-03  stapler validate on the dmg              no ticket                               valid ticket
  G-04  stapler validate on the app              no ticket                               no ticket (only the dmg is stapled; INFO)
  P-06  LSMinimumSystemVersion vs Mach-O minos   WARN if lower (v0.3.2: 10.13 vs 11.0)   same: WARN until the owner decides
  A-01  architecture                             arm64 only                              arm64 only (Intel not claimed)
  L-*   Licenses/, THIRD_PARTY_NOTICES, manifest present, manifest hashes match           same
Not asserted by any script here: Finder first launch under Gatekeeper, offline first launch, runtime behaviour
(see MANUAL-CHECKLIST.md).
TABLE

summary
L "transcript: $TRANSCRIPT"
L "(collect_evidence.sh records the sha256 of this transcript)"
[ "$N_FAIL" -eq 0 ]
