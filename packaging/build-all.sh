#!/usr/bin/env bash
# The full pinned build for the host OS, in dependency order (see build-notices.sh for why the sidecar is frozen twice).
# Nothing is signed or published. Output: packaging/dist/ (sidecar, build manifest, SBOM) and the launcher bundle.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"
# macOS: build only the .app by default; the dmg step drives Finder/AppleScript (set LAUNCHER_BUNDLES= to build everything)
if [ "$(uname -s)" = Darwin ]; then export LAUNCHER_BUNDLES="${LAUNCHER_BUNDLES-app}"; fi
"$HERE/build-wheelhouse.sh"
"$HERE/build-sidecar.sh"                                         # pass 1: the archive the notices are derived from
"$HERE/build-notices.sh"
REDEVOPS_LICENSES_DIR="$ROOT/deploy/launcher/src-tauri/licenses" "$HERE/build-sidecar.sh"   # pass 2: embeds Licenses
"$HERE/stage-sidecar.sh"
"$HERE/build-launcher.sh"
"$HERE/build-sbom.sh"
