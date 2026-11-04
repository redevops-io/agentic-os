#!/usr/bin/env bash
# Generate and verify the shipped-component SBOM (CycloneDX 1.5) from the BUILT artifacts.
# Run after build-sidecar.sh and build-launcher.sh. Reads packaging/dist/redevops-sidecar, the
# wheelhouse, the freeze-venv inventory, the launcher binary + Cargo.lock graph; writes
# packaging/dist/sbom/{sbom.cdx.json,archive-attribution.json,sbom-inputs.json,sbom-verify.txt}.
# Exits non-zero if the verifier finds an unlisted component.
set -euo pipefail
. "$(dirname "$0")/common.sh"
DIST="$PK/dist"
TARGET="${CARGO_TARGET_DIR:-$ROOT/deploy/launcher/src-tauri/target}"
LBIN="$TARGET/release/agentic-apps"; [ -f "$LBIN.exe" ] && LBIN="$LBIN.exe"
case "$OS" in macos) TRIPLE=aarch64-apple-darwin ;; windows) TRIPLE=x86_64-pc-windows-msvc ;; linux) TRIPLE=x86_64-unknown-linux-gnu ;; esac
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# PyInstaller (hash-locked) is needed to read the executable's archive
uv venv --python "$PYTHON_VERSION" "$TMP/sbomvenv" >/dev/null
SPY="$(venv_py "$TMP/sbomvenv")"
uv pip install --python "$SPY" --require-hashes --no-deps -r "$LOCKS/build-tools-$OS.lock" >/dev/null
ARGS=(--sidecar "$DIST/redevops-sidecar$([ "$OS" = windows ] && echo .exe)" --wheelhouse "$PK/wheelhouse"
      --inventory-json "$DIST/inventory-env.json" --build-manifest "$DIST/build-manifest.json" --git-pins "$PK/git-pins.json"
      --cargo-manifest "$ROOT/deploy/launcher/src-tauri/Cargo.toml" --cargo-lock "$ROOT/deploy/launcher/src-tauri/Cargo.lock"
      --cargo-target "$TRIPLE" --tauri-conf "$ROOT/deploy/launcher/src-tauri/tauri.conf.json")
[ -f "$LBIN" ] && ARGS+=(--launcher-bin "$LBIN")
"$SPY" -I "$PK/sbom/generate_sbom.py" "${ARGS[@]}" --out "$DIST/sbom"
"$SPY" -I "$PK/sbom/verify_sbom.py" --sbom "$DIST/sbom/sbom.cdx.json" "${ARGS[@]}" | tee "$DIST/sbom/sbom-verify.txt"
