#!/usr/bin/env bash
# Task 4 — build the Tauri launcher and the native installer(s) from PINNED inputs.
#
#   * rustc must be exactly $RUST_VERSION and `cargo tauri` exactly $TAURI_CLI_VERSION
#   * the crate graph is the committed deploy/launcher/src-tauri/Cargo.lock, enforced with --locked
#     (a Cargo.toml change that is not in the lock FAILS the build instead of silently re-resolving)
#   * LAUNCHER_BUNDLES (default: all targets in tauri.conf.json for the host) can be narrowed, e.g.
#     LAUNCHER_BUNDLES=app on macOS builds only the .app (no dmg: the dmg step drives Finder/AppleScript)
#
# Run after build-sidecar.sh + stage-sidecar.sh (externalBin). Extends packaging/dist/build-manifest.json.
set -euo pipefail
. "$(dirname "$0")/common.sh"
T0="$(now)"
LOCK="$ROOT/deploy/launcher/src-tauri/Cargo.lock"
[ -f "$LOCK" ] || { echo "Cargo.lock missing: run packaging/regen-cargo-lock.sh"; exit 1; }
[ "$(rustc --version | cut -d' ' -f2)" = "$RUST_VERSION" ] || { echo "rustc is '$(rustc --version)', pinned $RUST_VERSION"; exit 1; }
# tauri-cli is `cargo tauri` (cargo install tauri-cli, as CI does) or the equivalent `tauri` binary of the npm package @tauri-apps/cli
if cargo tauri --version >/dev/null 2>&1; then TAURI=(cargo tauri); elif command -v tauri >/dev/null 2>&1; then TAURI=(tauri); else echo "tauri-cli not found"; exit 1; fi
[ "$("${TAURI[@]}" --version | awk '{print $NF}')" = "$TAURI_CLI_VERSION" ] || { echo "tauri-cli is '$("${TAURI[@]}" --version)', pinned $TAURI_CLI_VERSION"; exit 1; }
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
cd "$ROOT/deploy/launcher"
BUNDLES=()
[ -n "${LAUNCHER_BUNDLES:-}" ] && BUNDLES=(--bundles "$LAUNCHER_BUNDLES")
# cargo's own args go after `--`.
"${TAURI[@]}" build ${BUNDLES[@]+"${BUNDLES[@]}"} -- --locked

LOCK_AFTER="$(shasum -a 256 "$LOCK" 2>/dev/null | cut -d' ' -f1 || sha256sum "$LOCK" | cut -d' ' -f1)"
[ "$LOCK_BEFORE" = "$LOCK_AFTER" ] || { echo "ERROR: Cargo.lock changed during the build"; exit 1; }

TARGET="${CARGO_TARGET_DIR:-$ROOT/deploy/launcher/src-tauri/target}"
BIN="$TARGET/release/agentic-apps"; [ -f "$BIN.exe" ] && BIN="$BIN.exe"
MPY="${MANIFEST_PYTHON:-$(command -v python3 || command -v python)}"
"$MPY" -I "$PK/tools/build_manifest.py" --root "$ROOT" --out "$PK/dist/build-manifest.json" --stage launcher \
    --artifact "launcher=$BIN" --artifact "launcher_bundles=$TARGET/release/bundle" \
    --cmd 'rustc="rustc -Vv"' --cmd 'cargo="cargo -V"' --cmd "tauri_cli=${TAURI[*]} --version" \
    --duration "launcher=$(( $(now) - T0 ))"
echo "launcher: $BIN"
