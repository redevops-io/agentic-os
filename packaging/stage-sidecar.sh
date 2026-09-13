#!/usr/bin/env bash
# Place the frozen sidecar where Tauri's `externalBin` expects it: a per-target-triple name under
# deploy/launcher/src-tauri/binaries/. Run after build-sidecar.sh, before `cargo tauri build`.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/deploy/launcher/src-tauri/binaries"
TRIPLE="$(rustc -Vv | sed -n 's/host: //p')"
mkdir -p "$DEST"
SRC="$ROOT/packaging/dist/redevops-sidecar"
EXT=""
[ -f "$SRC.exe" ] && { SRC="$SRC.exe"; EXT=".exe"; }
[ -f "$SRC" ] || { echo "sidecar missing — run packaging/build-sidecar.sh first"; exit 1; }
cp "$SRC" "$DEST/redevops-sidecar-$TRIPLE$EXT"
chmod +x "$DEST/redevops-sidecar-$TRIPLE$EXT" || true
echo "staged: binaries/redevops-sidecar-$TRIPLE$EXT"
