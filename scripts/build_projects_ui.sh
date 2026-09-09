#!/usr/bin/env bash
# Rebuild the vendored Projects UI from the redevops-projects repo and copy it into
# agentic_os/projects_ui/. The UI is built for the SAME origin as the API (VITE_PROJECTS_API=/)
# so the single service serves both with no CORS. Run this whenever the UI changes.
#
#   scripts/build_projects_ui.sh [path-to-redevops-projects]   (default: ../redevops-projects)
set -euo pipefail
UI_REPO="${1:-../redevops-projects}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$UI_REPO/package.json" ] || { echo "redevops-projects not found at $UI_REPO" >&2; exit 1; }
( cd "$UI_REPO" && VITE_PROJECTS_API=/ npm run build )
rm -rf "$ROOT/agentic_os/projects_ui"
mkdir -p "$ROOT/agentic_os/projects_ui"
cp -r "$UI_REPO/dist/"* "$ROOT/agentic_os/projects_ui/"
echo "vendored $UI_REPO/dist -> agentic_os/projects_ui/"
