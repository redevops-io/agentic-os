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
[ "$(cargo tauri --version | awk '{print $NF}')" = "$TAURI_CLI_VERSION" ] || { echo "cargo tauri is '$(cargo tauri --version)', pinned $TAURI_CLI_VERSION"; exit 1; }
LOCK_BEFORE="$(shasum -a 256 "$LOCK" 2>/dev/null | cut -d' ' -f1 || sha256sum "$LOCK" | cut -d' ' -f1)"

cd "$ROOT/deploy/launcher"
BUNDLES=()
[ -n "${LAUNCHER_BUNDLES:-}" ] && BUNDLES=(--bundles "$LAUNCHER_BUNDLES")
# cargo's own args go after `--`.
cargo tauri build ${BUNDLES[@]+"${BUNDLES[@]}"} -- --locked

LOCK_AFTER="$(shasum -a 256 "$LOCK" 2>/dev/null | cut -d' ' -f1 || sha256sum "$LOCK" | cut -d' ' -f1)"
[ "$LOCK_BEFORE" = "$LOCK_AFTER" ] || { echo "ERROR: Cargo.lock changed during the build"; exit 1; }

TARGET="${CARGO_TARGET_DIR:-$ROOT/deploy/launcher/src-tauri/target}"
BIN="$TARGET/release/agentic-apps"; [ -f "$BIN.exe" ] && BIN="$BIN.exe"
MPY="${MANIFEST_PYTHON:-python3}"
"$MPY" -I "$PK/tools/build_manifest.py" --root "$ROOT" --out "$PK/dist/build-manifest.json" --stage launcher \
    --artifact "launcher=$BIN" --artifact "launcher_bundles=$TARGET/release/bundle" \
    --cmd 'rustc="rustc -Vv"' --cmd 'cargo="cargo -V"' --cmd 'tauri_cli="cargo tauri --version"' \
    --duration "launcher=$(( $(now) - T0 ))"
echo "launcher: $BIN"
