# Source me: sets the variables the harness needs (edit S if the scratch root moves).
export S=/private/tmp/claude-501/-Users-arybach-agentic-os/30e85532-95c8-480d-aadc-1f976c943ab3/scratchpad
export GUI_WORK=$S/r0prime/gui-work
export GUI_DIR=$S/r0prime/wt-gui-tests/packaging/acceptance/macos-r0prime/gui   # absolute on purpose (BASH_SOURCE is empty under zsh)
[ -x "$GUI_DIR/shim/open" ] || { echo "FATAL: open shim missing at $GUI_DIR/shim/open" >&2; return 1 2>/dev/null || exit 1; }
export RDO_CHROMIUM=/Users/arybach/Library/Caches/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-mac-arm64/chrome-headless-shell
export RDO_PWCORE=$S/qa-impl/blog/redevops-io-web/node_modules/playwright-core/index.js
export APP="$GUI_WORK/app/Agentic Apps.app"
export RDO_SIDECAR="$APP/Contents/MacOS/redevops-sidecar"
export DRIVER=$GUI_WORK/target/release/gui-launcher-driver
export RDENV="$GUI_DIR/lib/rdenv.sh"
