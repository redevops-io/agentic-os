#!/bin/bash
# Provision a fresh Ubuntu 24.04 aarch64 VM as the Linux native build/qualification host (pins from packaging/toolchain.env)
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
sudo apt-get install -y build-essential curl git pkg-config ca-certificates dpkg-dev file libssl-dev libwebkit2gtk-4.1-dev libgtk-3-dev \
  librsvg2-dev libayatana-appindicator3-dev patchelf rsync libfuse2t64 xvfb xdotool x11-utils xauth xdg-utils squashfs-tools desktop-file-utils \
  zip unzip jq iproute2 nftables strace lsof netcat-openbsd psmisc imagemagick xclip dbus-x11 python3-venv python3-pip
. /tmp/toolchain.env
mkdir -p "$HOME/tools/bin"
curl --proto '=https' --tlsv1.2 -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" -o /tmp/uv-install.sh
UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh /tmp/uv-install.sh
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh
sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain "$RUST_VERSION"
. "$HOME/.cargo/env"
cargo install tauri-cli --version "$TAURI_CLI_VERSION" --locked
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
echo "--- versions"
uv --version; rustc --version; cargo --version; cargo tauri --version
