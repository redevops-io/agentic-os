#!/usr/bin/env bash
# Build the LOCAL macOS arm64 R0-prime candidate (UNSIGNED .app + .dmg) from the current checkout and assemble the
# evidence folder. Nothing is signed, notarized, uploaded or published; no keychain, identity or credential is used.
#
#   packaging/candidate/build-local-macos-candidate.sh OUT_DIR
#
# OUT_DIR must not exist or must be empty (a second candidate never overwrites the first). Needs on PATH: the pinned
# rustc/cargo (1.98.1), tauri-cli (`tauri` or `cargo tauri`, 2.11.4), uv, python3, git, network (PyPI/GitHub/crates.io).
# The script re-executes itself under `env -i` with an allowlist (PATH HOME USER TMPDIR CARGO_HOME ...), so the caller's
# credentials never reach the build. Example (one go, on a new tip):
#   packaging/candidate/build-local-macos-candidate.sh "$S/r0prime/candidate/macos-arm64-unsigned-<tip>"
#
# Steps: refuse a dirty tree / signing variables -> move previous build outputs aside (never deleted) -> build-all.sh
# (CI=true: the dmg bundler skips the Finder AppleScript styling) -> copy .app/.dmg/SBOM/notices/source offer/manifest ->
# list the dmg (read-only mount) -> Mach-O facts and minos of every embedded binary -> smoke test of the embedded
# sidecar (no GUI) -> README + SHA256SUMS. Unit/Rust/pytest suites are separate commands (see the build report).
set -euo pipefail
OUT="${1:?usage: build-local-macos-candidate.sh OUT_DIR}"
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"; PK="$ROOT/packaging"
[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || { echo "macOS arm64 only"; exit 2; }
if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT" 2>/dev/null)" ]; then echo "OUT_DIR $OUT exists and is not empty"; exit 2; fi
if env | grep -qiE '^(APPLE_|CODESIGN|TAURI_SIGNING|KEYCHAIN)'; then echo "refusing: a signing-related variable is set (names: $(env | grep -iE '^(APPLE_|CODESIGN|TAURI_SIGNING|KEYCHAIN)' | cut -d= -f1 | tr '\n' ' '))"; exit 2; fi
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ] && [ "${ALLOW_DIRTY:-0}" != 1 ]; then echo "tracked files are modified: commit first (the manifest records the commit), or ALLOW_DIRTY=1"; exit 2; fi
# Re-run once in an ALLOWLISTED environment: no ambient credential (API keys, tokens, cloud/agent sockets) reaches the build,
# the tests or the smoke run. (The signing-variable refusal above runs first, in the caller's environment.)
if [ -z "${RDO_CANDIDATE_CLEAN_ENV:-}" ]; then
  keep=(PATH="$PATH" HOME="$HOME" USER="${USER:-}" LANG=en_US.UTF-8 TERM=dumb RDO_CANDIDATE_CLEAN_ENV=1)
  for v in TMPDIR CARGO_HOME RUSTUP_HOME ALLOW_DIRTY UV_CACHE_DIR; do [ -n "${!v+x}" ] && keep+=("$v=${!v}"); done
  exec env -i "${keep[@]}" bash "$0" "$@"
fi
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
mkdir -p "$OUT"/{logs,sbom,notices,listings,environment,smoke-test,signing-inputs}
COMMIT="$(git -C "$ROOT" rev-parse HEAD)"; TREE="$(git -C "$ROOT" rev-parse 'HEAD^{tree}')"
. "$PK/toolchain.env"
BD="$ROOT/deploy/launcher/src-tauri/target/release/bundle"
APPNAME="Agentic Apps.app"

echo "== 1. move previous build outputs aside =="
PREV="$OUT/previous-outputs-moved-aside"
for d in packaging/dist packaging/build packaging/wheelhouse packaging/notices/out deploy/launcher/src-tauri/licenses deploy/launcher/src-tauri/binaries deploy/launcher/src-tauri/target/release/bundle; do
  if [ -e "$ROOT/$d" ]; then mkdir -p "$PREV/$(dirname "$d")"; mv "$ROOT/$d" "$PREV/$d"; fi
done

echo "== 2. build-all.sh (CI=true, app + dmg) =="
( cd "$ROOT" && CI=true LAUNCHER_BUNDLES=app,dmg "$PK/build-all.sh" ) 2>&1 | tee "$OUT/logs/build-all.log" | tail -5
[ -d "$BD/macos/$APPNAME" ] || { echo "no .app produced"; exit 1; }
DMG="$(ls "$BD"/dmg/*.dmg | head -1)"

echo "== 3. copy artifacts and evidence =="
ditto "$BD/macos/$APPNAME" "$OUT/$APPNAME"; cp "$DMG" "$OUT/"
DMGNAME="$(basename "$DMG")"
cp "$PK"/dist/sbom/* "$OUT/sbom/"; cp "$PK/dist/build-manifest.json" "$OUT/"; cp "$PK/dist/freeze-venv-pip-freeze.txt" "$OUT/environment/"
L="$ROOT/deploy/launcher/src-tauri/licenses"
cp "$L"/THIRD_PARTY_NOTICES.txt "$L"/NOTICES_MISSING.md "$L"/NOTICES_MISSING.json "$L"/LICENSES-MANIFEST.json "$OUT/notices/"
cp "$PK/notices/out/notices-report.json" "$OUT/notices/"; cp "$PK/notices/out/crate-sources/fetch-report.json" "$OUT/notices/crate-fetch-report.json"
cp "$PK/macos/sidecar.entitlements.plist" "$OUT/signing-inputs/"
python3 -I "$PK/sbom/generate_source_offer.py" --sbom "$PK/dist/sbom/sbom.cdx.json" --out "$OUT/SOURCE-OFFER.md" --template "$PK/SOURCE-OFFER.md" \
  --git-pins "$PK/git-pins.json" --sources "$PK/source-offer/third-party-sources.json" --build-manifest "$PK/dist/build-manifest.json"

echo "== 4. listings, dmg contents, Mach-O facts =="
APP="$OUT/$APPNAME"
(cd "$APP" && find . -type f | sort | while read -r f; do echo "$(shasum -a 256 "$f" | cut -d' ' -f1)  $(stat -f '%z %Sp' "$f")  $f"; done) > "$OUT/listings/app-contents.txt"
M="$(mktemp -d)"; hdiutil attach -readonly -nobrowse -noautoopen -noverify -mountpoint "$M" "$OUT/$DMGNAME" >/dev/null
( cd "$M" && find . -not -name ".DS_Store" \( -type f -o -type l \) | sort | while read -r f; do
    if [ -L "$f" ]; then echo "symlink  $f -> $(readlink "$f")"; else echo "$(shasum -a 256 "$f" | cut -d' ' -f1)  $(stat -f %z "$f")  $f"; fi; done ) > "$OUT/listings/dmg-contents.txt"
if diff <(cd "$M/$APPNAME" && find . -type f | sort | xargs shasum -a 256) <(cd "$APP" && find . -type f | sort | xargs shasum -a 256) >/dev/null; then
  echo "the .app inside the dmg has file hashes identical to the .app copy" > "$OUT/listings/dmg-vs-app.txt"; else echo "DIFFERENT" > "$OUT/listings/dmg-vs-app.txt"; fi
hdiutil detach "$M" >/dev/null; rmdir "$M"
{ echo "## Info.plist"; plutil -p "$APP/Contents/Info.plist"; echo; for f in "$APP/Contents/MacOS/"*; do echo "--- $(basename "$f")"; lipo -archs "$f"; vtool -show-build "$f" | grep -E "platform|minos|sdk"; codesign -dv "$f" 2>&1 | grep -E "Identifier|flags|Signature|TeamIdentifier"; echo "entitlements:"; codesign -d --entitlements - "$f" 2>&1 | grep -v '^Executable' || true; done
  echo "--- bundle"; codesign -dv "$APP" 2>&1 | grep -E "Format|Signature|Sealed|TeamIdentifier" || true; codesign --verify --deep --strict "$APP" 2>&1 | head -2 || true; } > "$OUT/listings/app-info-and-macho-facts.txt" 2>&1
TV="$(mktemp -d)"; uv venv --python "$PYTHON_VERSION" "$TV/v" >/dev/null
uv pip install --python "$TV/v/bin/python" --require-hashes --no-deps -r "$PK/locks/build-tools-macos.lock" >/dev/null
"$TV/v/bin/python" -I "$HERE/minos_scan.py" --sidecar "$APP/Contents/MacOS/redevops-sidecar" --out "$TV/scan" > "$OUT/listings/sidecar-embedded-macho-minos.tsv" 2> "$OUT/listings/sidecar-embedded-macho-minos.summary.txt" || true

echo "== 5. smoke test of the embedded sidecar (no GUI) =="
SW="$(mktemp -d)"
python3 -I "$HERE/check_app_smoke.py" --sidecar "$APP/Contents/MacOS/redevops-sidecar" --work "$SW" --log "$OUT/smoke-test/smoke_app.log" > /dev/null || echo "SMOKE FAILED (see smoke-test/smoke_app.log)"
cp "$SW/smoke-results.json" "$OUT/smoke-test/" 2>/dev/null || true
bash "$PK/check-sidecar-locales.sh" "$APP/Contents/MacOS/redevops-sidecar" > "$OUT/smoke-test/check-sidecar-locales.txt" 2>&1 || echo "locales check FAILED"
python3 "$PK/check-sidecar-lifecycle.py" "$APP/Contents/MacOS/redevops-sidecar" > "$OUT/smoke-test/check-sidecar-lifecycle.txt" 2>&1 || echo "lifecycle check FAILED"

echo "== 6. environment, README, SHA256SUMS =="
{ echo "LOCAL BUILD ENVIRONMENT, recorded $(date -u +%Y-%m-%dT%H:%M:%SZ); builder identity: local (this Mac), not CI"
  echo "--- host"; uname -a; sw_vers; sysctl -n machdep.cpu.brand_string; sysctl -n hw.model
  echo "--- toolchain"; rustc -Vv; cargo -V; command -v tauri || true; (tauri --version || cargo tauri --version) 2>&1 | head -1; uv --version; python3 --version
  PYX="$(uv python find "$PYTHON_VERSION")"; echo "--- interpreter uv resolved for $PYTHON_VERSION (python-build-standalone, NOT python.org / setup-python)"; echo "$PYX"
  "$PYX" -c "import platform,ssl;print(platform.python_version(), platform.python_build(), platform.python_compiler());print(ssl.OPENSSL_VERSION)"; shasum -a 256 "$PYX"
  echo "--- Apple developer tools"; xcode-select -p; pkgutil --pkg-info com.apple.pkg.CLTools_Executables 2>/dev/null | head -3 || true; clang --version | head -2; xcrun --show-sdk-version 2>/dev/null || true
  echo "--- hash-locked build tools"; uv pip list --python "$TV/v/bin/python" 2>/dev/null
  echo "--- signing-related environment variable NAMES (none allowed)"; env | grep -iE '^(APPLE|CODESIGN|TAURI_SIGNING|KEYCHAIN)' | cut -d= -f1 || true
} > "$OUT/environment/local-build-environment.txt" 2>&1
DMGSHA="$(shasum -a 256 "$OUT/$DMGNAME" | cut -d' ' -f1)"
cat > "$OUT/README-LOCAL-BUILD.md" <<EOF
# Agentic Apps R0-prime candidate, macOS arm64, UNSIGNED, LOCAL BUILD (not CI)

| | |
|---|---|
| Builder identity | **local**: the developer Mac named in \`environment/local-build-environment.txt\`. NOT a GitHub Actions run. |
| Commit / tree | \`$COMMIT\` / \`$TREE\` (build manifest: \`build-manifest.json\`) |
| Built | $(date -u +%Y-%m-%dT%H:%M:%SZ) |
| Signing | **none**. No identity, keychain, notarization or stapling was used; the .app bundle has no seal (\`codesign --verify\` fails by design) and carries only the linker/ad-hoc signatures of its Mach-O files. Gatekeeper will refuse it on another Mac. |
| dmg | \`$DMGNAME\`, sha256 \`$DMGSHA\`. Built with CI=true: the Finder/AppleScript window styling is skipped, so the disk image has no custom layout. The dmg bytes are not reproducible (timestamps); the .app file hashes inside are listed in \`listings/\`. |
| Toolchain | CPython $PYTHON_VERSION, PyInstaller $PYINSTALLER_VERSION, rustc $RUST_VERSION, tauri-cli $TAURI_CLI_VERSION (from \`packaging/toolchain.env\`; actual local versions in \`environment/\`). |

## What differs from the frozen CI build of v0.3.2 (and from a future CI build of this commit)

* **Interpreter:** uv's python-build-standalone CPython $PYTHON_VERSION (OpenSSL 3.0.16 linked into libpython; 6 embedded Mach-O files). The frozen CI build used the macOS runner's python.org framework build (Python.framework, separate libcrypto/libssl). The pinned CI workflow installs the same version with actions/setup-python; whether that equals the frozen interpreter is UNVERIFIED (decision pins-06).
* **Machine:** a local Mac (see environment file), not the GitHub \`macos-14\` runner image; local SDK/Command Line Tools instead of the runner's Xcode; local uv may differ from the pinned uv in \`toolchain.env\` (recorded).
* **tauri-cli:** the npm package \`@tauri-apps/cli\` binary (same version number) instead of \`cargo install tauri-cli\`.
* **Cargo.lock** is the reconstructed lock (decision pins-11); crate sources and licence texts were downloaded here (checksum-verified).
* Nothing is published and nothing leaves this machine.

## Contents

\`$APPNAME/\` (the bundle), \`$DMGNAME\`, \`sbom/\` (CycloneDX + verification), \`notices/\` (THIRD_PARTY_NOTICES, NOTICES_MISSING.*: open items are decision items), \`SOURCE-OFFER.md\`, \`build-manifest.json\`, \`listings/\` (every file in the .app and dmg with sha256, Mach-O facts, minos of the embedded libraries), \`smoke-test/\` (sidecar run without the GUI), \`signing-inputs/sidecar.entitlements.plist\` (input for a later signing run, see docs/MACOS_ENTITLEMENTS.md), \`environment/\`, \`logs/\`, \`SHA256SUMS\`.

## Would need signing later (not done)

1. \`Contents/MacOS/redevops-sidecar\` (Mach-O, with the entitlement plist, hardened runtime) and the Mach-O libraries it extracts at run time if library validation is to stay on (otherwise the entitlement applies);
2. \`Contents/MacOS/agentic-apps\` (launcher Mach-O, no entitlements);
3. the app bundle \`$APPNAME\` (inside-out, after 1 and 2) and notarization + stapling;
4. the dmg (sign, notarize, staple).
Windows msi/nsis/exe and installed executables are not built here.
EOF
(cd "$OUT" && find . -type f ! -name SHA256SUMS ! -path './previous-outputs-moved-aside/*' | sort | while read -r f; do shasum -a 256 "$f"; done) > "$OUT/SHA256SUMS"
echo "done: $OUT  (dmg sha256 $DMGSHA)"
