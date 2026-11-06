# Source me. Every path comes from the environment; nothing is hard-coded to a user or a scratch root.
#   S             scratch root (absolute)                          GUI_WORK   disposable work area (default $S/r0prime/gui-work)
#   GUI_DIR       absolute path of THIS gui/ directory (BASH_SOURCE is empty under zsh, so it is not derived)
#   REAL_HOME     the real user's home (only for lib/snap_real.sh, which reads names and mtimes)
#   RDO_CHROMIUM  path of a headless Chromium (chrome-headless-shell) used behind the `open` shim
#   RDO_PWCORE    path of playwright-core's index.js
: "${S:?set S (scratch root, absolute)}"
: "${GUI_DIR:?set GUI_DIR (absolute path of packaging/acceptance/macos-r0prime/gui)}"
: "${RDO_CHROMIUM:?set RDO_CHROMIUM (path of the headless Chromium)}"
: "${RDO_PWCORE:?set RDO_PWCORE (path of playwright-core/index.js)}"
export S GUI_DIR RDO_CHROMIUM RDO_PWCORE
export GUI_WORK="${GUI_WORK:-$S/r0prime/gui-work}"
[ -x "$GUI_DIR/shim/open" ] || { echo "FATAL: open shim missing at $GUI_DIR/shim/open" >&2; return 1 2>/dev/null || exit 1; }
export APP="${APP:-$GUI_WORK/app/Agentic Apps R0-prime.app}"
export RDO_SIDECAR="${RDO_SIDECAR:-$APP/Contents/MacOS/redevops-sidecar}"
export DRIVER="${DRIVER:-$GUI_WORK/target/release/gui-launcher-driver}"
export RDENV="$GUI_DIR/lib/rdenv.sh"
