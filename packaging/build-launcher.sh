#!/usr/bin/env bash
# Task 4 — build the Tauri launcher and the native installer(s) from PINNED inputs.
#
#   * rustc must be exactly $RUST_VERSION and `cargo tauri` exactly $TAURI_CLI_VERSION
#   * the crate graph is the committed deploy/launcher/src-tauri/Cargo.lock, enforced with --locked
#     (a Cargo.toml change that is not in the lock FAILS the build instead of silently re-resolving)
#   * LAUNCHER_BUNDLES (default: all targets in tauri.conf.json for the host) can be narrowed, e.g.
#     LAUNCHER_BUNDLES=app on macOS builds only the .app (no dmg: the dmg step drives Finder/AppleScript)
#   * LAUNCHER_ADHOC_SIGN=1 (macOS only; the private unsigned candidate script sets it): the bundler seals the .app with the AD-HOC
#     identity `-` (sidecar and launcher first, then the bundle), no hardened runtime, no entitlements, no timestamp, no keychain. Without
#     it the bundle carries only the linker's signature on the launcher and `codesign --verify --deep --strict` fails ("code has no
#     resources but signature indicates they must be present"), which Gatekeeper reports as "is damaged" for a quarantined download
#     instead of the normal "unidentified developer" path. This is NOT release signing: the reviewed Developer ID procedure
#     (packaging/signing/**) re-signs every file with --force. tauri.conf.json is not changed (tests/test_macos_entitlements.py).
#
# Run after build-sidecar.sh + stage-sidecar.sh (externalBin). Extends packaging/dist/build-manifest.json.
set -euo pipefail
. "$(dirname "$0")/common.sh"
T0="$(now)"
LOCK="$ROOT/deploy/launcher/src-tauri/Cargo.lock"
[ -f "$LOCK" ] || { echo "Cargo.lock missing: run packaging/regen-cargo-lock.sh"; exit 1; }
[ "$(rustc --version | cut -d' ' -f2)" = "$RUST_VERSION" ] || { echo "rustc is '$(rustc --version)', pinned $RUST_VERSION"; exit 1; }
# The Windows locks, the SBOM target (build-notices.sh / build-sbom.sh) and the staged sidecar name are all x86_64-pc-windows-msvc:
# refuse an ARM64 or GNU toolchain instead of producing a launcher that does not match them.
if [ "$OS" = windows ]; then
  HOST_TRIPLE="$(rustc -Vv | sed -n 's/^host: //p' | strip_cr)"
  [ "$HOST_TRIPLE" = x86_64-pc-windows-msvc ] || { echo "ERROR: rustc host is '$HOST_TRIPLE', the Windows build needs x86_64-pc-windows-msvc"; exit 1; }
fi
# tauri-cli is `cargo tauri` (cargo install tauri-cli, as CI does) or the equivalent `tauri` binary of the npm package @tauri-apps/cli
if cargo tauri --version >/dev/null 2>&1; then TAURI=(cargo tauri); elif command -v tauri >/dev/null 2>&1; then TAURI=(tauri); else echo "tauri-cli not found"; exit 1; fi
[ "$("${TAURI[@]}" --version | awk '{print $NF}')" = "$TAURI_CLI_VERSION" ] || { echo "tauri-cli is '$("${TAURI[@]}" --version)', pinned $TAURI_CLI_VERSION"; exit 1; }
# Order guard (build-all.sh): the notices must exist before the launcher is built (tauri-build bundles
# src-tauri/licenses as Licenses/ and stops without it) and the staged sidecar must be the pass-2 binary that
# embeds the SAME Licenses folder.
LIC="$ROOT/deploy/launcher/src-tauri/licenses"
[ -f "$LIC/THIRD_PARTY_NOTICES.txt" ] && [ -f "$LIC/LICENSES-MANIFEST.json" ] || {
  echo "ERROR: $LIC is missing or incomplete: run packaging/build-notices.sh (build-all.sh does) before the launcher"; exit 1; }
LICSHA="$("${MANIFEST_PYTHON:-$(command -v python3 || command -v python)}" -I -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$LIC/LICENSES-MANIFEST.json" | strip_cr)"
[ -f "$PK/dist/sidecar-licenses.sha256" ] && [ "$(strip_cr < "$PK/dist/sidecar-licenses.sha256")" = "$LICSHA" ] || {
  echo "ERROR: the sidecar in packaging/dist does not embed the Licenses folder that will be bundled (pass 2 of build-sidecar.sh with REDEVOPS_LICENSES_DIR, then stage-sidecar.sh)"; exit 1; }
cmp -s "$PK/dist/redevops-sidecar" "$ROOT/deploy/launcher/src-tauri/binaries/redevops-sidecar-$(rustc -Vv | sed -n 's/host: //p')" 2>/dev/null \
  || cmp -s "$PK/dist/redevops-sidecar.exe" "$ROOT/deploy/launcher/src-tauri/binaries/redevops-sidecar-$(rustc -Vv | sed -n 's/host: //p').exe" 2>/dev/null \
  || { echo "ERROR: the staged sidecar (src-tauri/binaries) is not the one in packaging/dist: run packaging/stage-sidecar.sh"; exit 1; }
LOCK_BEFORE="$(shasum -a 256 "$LOCK" 2>/dev/null | cut -d' ' -f1 || sha256sum "$LOCK" | cut -d' ' -f1)"

# rustc embeds absolute source paths (panic locations, file!()): the CARGO_HOME of the build host
# (`<cargo home>/registry/src/index.crates.io-*/tauri-2.11.5/src/...`, 300+ strings in the launcher) and the
# checkout dir. Remap them to fixed prefixes so two hosts produce the same bytes. (The frozen v0.3.2 launcher
# carries `/Users/runner/.cargo/...`.) Set LAUNCHER_REMAP_PATHS=0 to build without. POSIX hosts only: the
# Windows path forms (C:\Users\...) under git-bash are not handled here (UNVERIFIED on Windows).
if [ "${LAUNCHER_REMAP_PATHS:-1}" = 1 ] && [ "$OS" != windows ]; then
  CH="${CARGO_HOME:-$HOME/.cargo}"
  export RUSTFLAGS="${RUSTFLAGS:+$RUSTFLAGS }--remap-path-prefix=$CH=/cargo-home --remap-path-prefix=$ROOT=/src"
  echo "launcher: RUSTFLAGS=$RUSTFLAGS"
fi
# Windows: link.exe stamps the PE TimeDateStamp, the debug-directory timestamps and a random PDB GUID into agentic-r0prime.exe, so two builds of the
# same tree on the same host differed in 24 bytes (2026-11-05, Windows Server 2022: sidecar byte-identical, launcher not; NSIS/MSI differ downstream).
# /Brepro makes them a function of the content. It changes no mitigation (ASLR/DEP/CFG flags are unaffected). LAUNCHER_BREPRO=0 builds without.
if [ "$OS" = windows ] && [ "${LAUNCHER_BREPRO:-1}" = 1 ]; then
  export RUSTFLAGS="${RUSTFLAGS:+$RUSTFLAGS }-C link-arg=/Brepro"
  echo "launcher: RUSTFLAGS=$RUSTFLAGS"
fi
cd "$ROOT/deploy/launcher"
BUNDLES=()
[ -n "${LAUNCHER_BUNDLES:-}" ] && BUNDLES=(--bundles "$LAUNCHER_BUNDLES")
if [ "${LAUNCHER_ADHOC_SIGN:-0}" = 1 ]; then
  [ "$OS" = macos ] || { echo "ERROR: LAUNCHER_ADHOC_SIGN=1 is for macOS only"; exit 1; }
  # never next to a real signing identity: the ad-hoc seal must not be confused with (or replaced by) a release signature
  if env | grep -qE '^(APPLE_SIGNING_IDENTITY|APPLE_CERTIFICATE|APPLE_ID|APPLE_API_KEY|APPLE_TEAM_ID)='; then echo "ERROR: LAUNCHER_ADHOC_SIGN=1 with an Apple signing variable set"; exit 1; fi
  BUNDLES+=(--config '{"bundle":{"macOS":{"signingIdentity":"-","hardenedRuntime":false}}}')
fi
# cargo's own args go after `--`.
"${TAURI[@]}" build ${BUNDLES[@]+"${BUNDLES[@]}"} -- --locked

LOCK_AFTER="$(shasum -a 256 "$LOCK" 2>/dev/null | cut -d' ' -f1 || sha256sum "$LOCK" | cut -d' ' -f1)"
[ "$LOCK_BEFORE" = "$LOCK_AFTER" ] || { echo "ERROR: Cargo.lock changed during the build"; exit 1; }

TARGET="${CARGO_TARGET_DIR:-$ROOT/deploy/launcher/src-tauri/target}"
BIN="$TARGET/release/$(identity main_binary_name)"; [ -f "$BIN.exe" ] && BIN="$BIN.exe"
MPY="${MANIFEST_PYTHON:-$(command -v python3 || command -v python)}"
"$MPY" -I "$PK/tools/build_manifest.py" --root "$ROOT" --out "$PK/dist/build-manifest.json" --stage launcher \
    --artifact "launcher=$BIN" --artifact "launcher_bundles=$TARGET/release/bundle" --artifact "licenses_manifest=$LIC/LICENSES-MANIFEST.json" \
    --cmd 'rustc="rustc -Vv"' --cmd 'cargo="cargo -V"' --cmd "tauri_cli=${TAURI[*]} --version" \
    --duration "launcher=$(( $(now) - T0 ))"
echo "launcher: $BIN"
