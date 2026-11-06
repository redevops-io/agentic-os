#!/usr/bin/env bash
# The UI source of truth is github.com/redevops-io/redevops-projects; agentic_os/projects_ui/ is only a build output.
# DEV ONLY: build the Projects UI from a local redevops-projects clone into agentic_os/projects_ui/ (git-ignored;
# releases use the pinned bundle: scripts/fetch_projects_ui.py). The UI is built for the SAME origin as the API
# so the single service serves both with no CORS.
#
#   scripts/build_projects_ui.sh [path-to-redevops-projects] [base]
#     base "/"           → serve at the origin root (default) — pair with no PROJECTS_BASE_PATH
#     base "/projects/"  → serve under a sub-path        — pair with PROJECTS_BASE_PATH=/projects
set -euo pipefail
UI_REPO="${1:-../redevops-projects}"
BASE="${2:-/}"
API_BASE="${BASE%/}"; [ -z "$API_BASE" ] && API_BASE="/"    # VITE_PROJECTS_API: "/" or "/projects"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$UI_REPO/package.json" ] || { echo "redevops-projects not found at $UI_REPO" >&2; exit 1; }
( cd "$UI_REPO" && VITE_PROJECTS_API="$API_BASE" npm run build -- --base="$BASE" )
# hero.jpg here is the neutral placeholder drawn by scripts/make_neutral_hero.py (the image that used to be
# vendored carried third-party generator metadata and unread terms): keep it across re-vendoring.
HERO_KEEP="$(mktemp)"; cp "$ROOT/agentic_os/projects_ui/hero.jpg" "$HERO_KEEP"
rm -rf "$ROOT/agentic_os/projects_ui"
mkdir -p "$ROOT/agentic_os/projects_ui"
cp -r "$UI_REPO/dist/"* "$ROOT/agentic_os/projects_ui/"
# mark it as a deliberate local build so scripts/fetch_projects_ui.py (wheel-build hook) does not replace it
echo "local-build" > "$ROOT/agentic_os/projects_ui/.pinned-sha256"
cp "$HERO_KEEP" "$ROOT/agentic_os/projects_ui/hero.jpg"; rm -f "$HERO_KEEP"
echo "vendored $UI_REPO/dist (base=$BASE, api=$API_BASE) -> agentic_os/projects_ui/"
