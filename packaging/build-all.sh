#!/usr/bin/env bash
# The full pinned build for the host OS, in dependency order (see build-notices.sh for why the sidecar is frozen twice).
# Nothing is signed or published. Output: packaging/dist/ (sidecar, build manifest, SBOM) and the launcher bundle.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"
# Distribution gate: any REDEVOPS_DISTRIBUTION_BUILD other than empty or 0 is a distribution build and is refused (exit 3) while a distribution-blocking decision is OPEN
# (packaging/distribution-gate.json); a private build only prints the open decisions.
case "$(uname -s)" in Darwin) GATE_OS=macos ;; Linux) GATE_OS=linux ;; *) GATE_OS=windows ;; esac
# --about-slots: also resolve the About page's legal slots (a DECIDED slot whose approved text is missing or does not match is an error in
# every mode; a distribution build with a slot still unfilled is refused). generate_about.py (build-notices.sh) enforces the same rules.
"${MANIFEST_PYTHON:-python3}" -I "$HERE/check-distribution-gate.py" --platform "$GATE_OS" --about-slots || exit 3
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
# closing check of the About record: consistent with identity, versions, the notices, the manifest, the gate, and byte-identical
# to every copy inside the launcher bundle (the sidecar's embedded copy is covered by the SBOM comparison above)
COPIES=(); while IFS= read -r f; do COPIES+=(--copy "$f"); done < <(find "${CARGO_TARGET_DIR:-$ROOT/deploy/launcher/src-tauri/target}/release/bundle" -path '*Licenses/about.json' 2>/dev/null)
"${MANIFEST_PYTHON:-python3}" -I "$HERE/notices/check_about.py" --licenses-dir "$ROOT/deploy/launcher/src-tauri/licenses" \
    --notices-report "$HERE/notices/out/notices-report.json" ${COPIES[@]+"${COPIES[@]}"}
