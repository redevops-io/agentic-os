#!/bin/sh
# Called by the `app` part of snap/snapcraft.yaml (override-build). Builds the SAME .deb payload as packaging/build-all.sh does
# for the Linux .deb, then unpacks it into the part install directory. Runs the pinned pipeline under an `env -i` allowlist
# (snapcraft's own build environment exports PYTHONPATH / LD_LIBRARY_PATH / PATH entries that made the pinned-pip check of
# build-wheelhouse.sh see the distribution's pip 24.0 instead of the hash-locked 26.2.1).
# Usage: build-payload.sh <part src dir> <part build dir> <part install dir>
set -eu
SRC="$1"; BUILD="$2"; INSTALL="$3"
cd "$SRC"
. packaging/toolchain.env
T="$BUILD/tools"; mkdir -p "$T/bin" "$BUILD/home" "$BUILD/tmp"
ENVV="PATH=$T/bin:$T/cargo/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=$BUILD/home TMPDIR=$BUILD/tmp LANG=C.UTF-8 TERM=dumb \
CARGO_HOME=$T/cargo RUSTUP_HOME=$T/rustup UV_INSTALL_DIR=$T/bin UV_NO_MODIFY_PATH=1 UV_PYTHON_PREFERENCE=managed LAUNCHER_BUNDLES=deb"
# shellcheck disable=SC2086
run() { env -i $ENVV "$@"; }
# the toolchain lives in the part build dir, never on the host; every version is re-checked by the pipeline's own scripts
curl --proto '=https' --tlsv1.2 -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" -o "$T/uv-install.sh"
run sh "$T/uv-install.sh"
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o "$T/rustup-init.sh"
run sh "$T/rustup-init.sh" -y --no-modify-path --profile minimal --default-toolchain "$RUST_VERSION"
run cargo install tauri-cli --version "$TAURI_CLI_VERSION" --locked
run python3 -I packaging/check-distribution-gate.py --platform linux --about-slots
run packaging/build-wheelhouse.sh
run packaging/build-sidecar.sh
run packaging/build-notices.sh
run env REDEVOPS_LICENSES_DIR="$SRC/deploy/launcher/src-tauri/licenses" packaging/build-sidecar.sh
run packaging/stage-sidecar.sh
run packaging/build-launcher.sh
DEB="$(ls deploy/launcher/src-tauri/target/release/bundle/deb/*.deb)"
dpkg-deb -x "$DEB" "$INSTALL"
install -Dm755 snap/local/agentic-apps-snap "$INSTALL/bin/agentic-apps-snap"
