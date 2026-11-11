#!/bin/bash
# vm_collect.sh LABEL NAME : copy the artifacts of build LABEL into ~/art/NAME and write local hashes. Runs INSIDE the build VM.
set -euo pipefail
LABEL="$1"; NAME="$2"; B="$HOME/$LABEL/src"; A="$HOME/art/$NAME"; rm -rf "$A"; mkdir -p "$A"
BD="$B/deploy/launcher/src-tauri/target/release/bundle"
shopt -s nullglob
debs=("$BD"/deb/*.deb); imgs=("$BD"/appimage/*.AppImage)
[ "${#debs[@]}" = 1 ] && [ "${#imgs[@]}" = 1 ] || { echo "expected exactly one .deb and one AppImage, found ${#debs[@]} and ${#imgs[@]}"; exit 9; }
cp "${debs[0]}" "$A/$(basename "${debs[0]}")"; cp "${imgs[0]}" "$A/$(basename "${imgs[0]}")"
cp "$B/packaging/dist/redevops-sidecar" "$A/redevops-sidecar"
cp "$B/deploy/launcher/src-tauri/target/release/agentic-r0prime" "$A/agentic-r0prime"
cp "$B/packaging/notices/out/about.json" "$A/about.json"
cp "$B/packaging/dist/build-manifest.json" "$A/build-manifest.json"
cp "$B/packaging/dist/sbom/sbom.cdx.json" "$A/sbom.cdx.json"
cp "$B/packaging/dist/sbom/sbom-verify.txt" "$A/sbom-verify.txt"
cp "$B/packaging/wheelhouse/WHEELS.sha256" "$A/WHEELS.sha256"
cp "$B/deploy/launcher/src-tauri/licenses/LICENSES-MANIFEST.json" "$A/LICENSES-MANIFEST.json"
cp "$B/deploy/launcher/src-tauri/licenses/THIRD_PARTY_NOTICES.txt" "$A/THIRD_PARTY_NOTICES.txt"
cp "$HOME/build-$LABEL.log" "$A/build.log"
( cd "$A" && sha256sum * > SHA256SUMS.local && ls -l )
