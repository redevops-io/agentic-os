#!/usr/bin/env bash
# Build the Licenses folder (THIRD_PARTY_NOTICES.txt, NOTICES_MISSING.*, product licence files) from the
# SBOM of the BUILT sidecar archive (pass 1) and stage it where the launcher bundle and the sidecar spec expect it
# (deploy/launcher/src-tauri/licenses). Wraps packaging/notices/build-licenses.sh (agent `licences`).
#
# Order of a full build (packaging/build-all.sh does this):
#   build-wheelhouse.sh -> build-sidecar.sh (pass 1) -> build-notices.sh -> build-sidecar.sh (pass 2, embeds Licenses:
#   REDEVOPS_LICENSES_DIR) -> stage-sidecar.sh -> build-launcher.sh (tauri-build needs licenses/ to exist) -> build-sbom.sh
# The component list does not change between sidecar pass 1 and 2 (only the embedded Licenses data files do).
#
# NOTICES_EXCLUDE (default: knowledge-frontier, excluded from the frozen sidecar: decision D-LIC-14) is passed
# to the generator. Needs network for `cargo fetch --locked` (crate sources for the licence texts).
set -euo pipefail
. "$(dirname "$0")/common.sh"
DIST="$PK/dist"; WH="$PK/wheelhouse"
case "$OS" in macos) TRIPLE=aarch64-apple-darwin ;; windows) TRIPLE=x86_64-pc-windows-msvc ;; linux) TRIPLE=x86_64-unknown-linux-gnu ;; esac
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
EXE=""; if [ "$OS" = windows ]; then EXE=".exe"; fi
# 1. pass-1 SBOM from the built sidecar (needs PyInstaller to read the archive: hash-locked tool venv)
uv venv --python "$PYTHON_VERSION" "$TMP/tools" >/dev/null; TPY="$(venv_py "$TMP/tools")"
uv pip install --python "$TPY" --require-hashes --no-deps -r "$LOCKS/build-tools-$OS.lock" >/dev/null
"$TPY" -I "$PK/sbom/generate_sbom.py" --sidecar "$DIST/redevops-sidecar$EXE" --wheelhouse "$WH" --inventory-json "$DIST/inventory-env.json" \
    --build-manifest "$DIST/build-manifest.json" --git-pins "$PK/git-pins.json" \
    --cargo-manifest "$ROOT/deploy/launcher/src-tauri/Cargo.toml" --cargo-lock "$ROOT/deploy/launcher/src-tauri/Cargo.lock" \
    --cargo-target "$TRIPLE" --tauri-conf "$ROOT/deploy/launcher/src-tauri/tauri.conf.json" --out "$DIST/sbom-pass1"
# 2. site-packages of the shipped set (dist-info licence files) from the wheelhouse, offline and hash-checked
uv venv --python "$PYTHON_VERSION" "$TMP/site" >/dev/null; SPY="$(venv_py "$TMP/site")"
uv pip install --python "$SPY" --no-index --find-links "$WH" --require-hashes --no-deps -r "$LOCKS/requirements-$OS.lock" >/dev/null
FIRST="$(ls "$WH"/agentic_os-*.whl "$WH"/runtime_contracts-*.whl "$WH"/knowledge_frontier-*.whl "$WH"/redevops_connectors-*.whl)"
# shellcheck disable=SC2086
uv pip install --python "$SPY" --no-index --find-links "$WH" --no-deps $FIRST >/dev/null
SITE="$("$SPY" -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])')"
# 3. crate sources for the licence texts
cargo fetch --locked --manifest-path "$ROOT/deploy/launcher/src-tauri/Cargo.toml"
CH="${CARGO_HOME:-$HOME/.cargo}"
CRATES=(); for d in "$CH"/registry/src/index.crates.io-*; do [ -d "$d" ] && CRATES+=(--crates-dir "$d"); done
EXCL=(); for x in ${NOTICES_EXCLUDE-knowledge-frontier}; do EXCL+=(--exclude "$x"); done
# 4. notices + staging (writes deploy/launcher/src-tauri/licenses)
"$PK/notices/build-licenses.sh" --platform "$OS" --sbom "$DIST/sbom-pass1/sbom.cdx.json" --site-packages "$SITE" \
    ${CRATES[@]+"${CRATES[@]}"} ${EXCL[@]+"${EXCL[@]}"}
echo "Licenses staged in $ROOT/deploy/launcher/src-tauri/licenses (set REDEVOPS_LICENSES_DIR to that path for sidecar pass 2)"
