#!/usr/bin/env bash
# make_adhoc_fixture.sh SOURCE_APP OUT_DIR
#
# Builds an UNSIGNED-style test input for `sign_and_notarize_r0prime_macos_v3.sh --adhoc-test` from an existing .app:
#   * SOURCE_APP is only READ (ditto out of it). It may be a read-only candidate; nothing in it is modified.
#   * If SOURCE_APP already has the R0-prime name (`<product>.app` from packaging/identity.json) it is used as it is.
#     Otherwise (for example the frozen-named final2 candidate) the COPY is renamed and patched to the R0-prime identity
#     (executable name, Info.plist identifier/name/version, RedevopsReleaseLine): a STAND-IN for the rc2 candidate that exists
#     only to exercise the signing sequence. The result is not a build of anything and must never be distributed.
#   * OUT_DIR (must not exist) receives input/<R0-prime dmg name> (UDZO dmg with the app, .VolumeIcon.icns, Applications link)
#     and prints its sha256. Nothing is signed. No network, no keychain.
set -euo pipefail
umask 077
[ $# -eq 2 ] || { echo "usage: $0 SOURCE_APP OUT_DIR" >&2; exit 2; }
SRC="$1"; OUT="$2"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
IDJ="$HERE/../../identity.json"
idj() { plutil -extract "$1" raw -o - "$IDJ"; }
PRODUCT="$(idj r0prime.product_name)"; VERSION="$(idj r0prime.version)"; BID="$(idj r0prime.bundle_identifier)"
MAIN="$(idj r0prime.main_binary_name)"; LINE="$(idj release_line)"
[ -d "$SRC" ] || { echo "SOURCE_APP is not a directory" >&2; exit 2; }
[ ! -e "$OUT" ] || { echo "OUT_DIR exists; use a new directory" >&2; exit 2; }
mkdir -p "$OUT/fixture" "$OUT/input"
APP="$OUT/fixture/$PRODUCT.app"
ditto "$SRC" "$APP"
if [ "$(basename "$SRC")" != "$PRODUCT.app" ]; then
  OLDEXE="$(plutil -extract CFBundleExecutable raw -o - "$APP/Contents/Info.plist")"
  [ "$OLDEXE" = "$MAIN" ] || mv "$APP/Contents/MacOS/$OLDEXE" "$APP/Contents/MacOS/$MAIN"
  P="$APP/Contents/Info.plist"
  plutil -replace CFBundleExecutable -string "$MAIN" "$P"
  plutil -replace CFBundleIdentifier -string "$BID" "$P"
  plutil -replace CFBundleName -string "$PRODUCT" "$P"
  plutil -replace CFBundleDisplayName -string "$PRODUCT" "$P"
  plutil -replace CFBundleShortVersionString -string "$VERSION" "$P"
  plutil -replace CFBundleVersion -string "$(idj r0prime.bundle_version)" "$P"
  plutil -replace RedevopsReleaseLine -string "$LINE" "$P"
fi
xattr -cr "$APP" 2>/dev/null || true
ICON="$APP/Contents/Resources/icon.icns"
[ -f "$ICON" ] && cp -p "$ICON" "$OUT/fixture/.VolumeIcon.icns"
ln -s /Applications "$OUT/fixture/Applications"
DMG="$OUT/input/${PRODUCT}_${VERSION}_aarch64.dmg"
hdiutil create -srcfolder "$OUT/fixture" -volname "$PRODUCT" -fs HFS+ -format UDZO -ov "$DMG" >/dev/null
echo "$DMG"
shasum -a 256 "$DMG" | awk '{print $1}'
