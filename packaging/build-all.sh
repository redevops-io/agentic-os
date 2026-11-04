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
# closing check: the staged notices describe exactly the components of the FINAL SBOM (sidecar archive + launcher)
EXCL=(); for x in ${NOTICES_EXCLUDE-knowledge-frontier}; do EXCL+=(--exclude "$x"); done
"${MANIFEST_PYTHON:-python3}" -I "$HERE/notices/check_notices_vs_sbom.py" --sbom "$HERE/dist/sbom/sbom.cdx.json" \
    --notices-report "$HERE/notices/out/notices-report.json" --licenses-dir "$ROOT/deploy/launcher/src-tauri/licenses" \
    ${EXCL[@]+"${EXCL[@]}"}
