#!/bin/bash
# Run a command under the disposable GUI-test environment (env -i allowlist; nothing inherited).
#   GUI_WORK   scratch root (default: $S/r0prime/gui-work)      GUI_HOME  disposable home (default $GUI_WORK/home)
#   GUI_TMP    disposable TMPDIR (default $GUI_WORK/tmp)         GUI_EXTRA  extra "NAME=value" words appended
# Usage: rdenv.sh <command> [args...]
set -u
GUI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${GUI_WORK:?set GUI_WORK to the scratch root}"
GUI_HOME="${GUI_HOME:-$GUI_WORK/home}"
GUI_TMP="${GUI_TMP:-$GUI_WORK/tmp}"
mkdir -p "$GUI_HOME" "$GUI_TMP" "$GUI_WORK/run" "$GUI_WORK/browserprof"
exec /usr/bin/env -i \
  HOME="$GUI_HOME" CFFIXED_USER_HOME="$GUI_HOME" TMPDIR="$GUI_TMP" \
  XDG_CONFIG_HOME="$GUI_HOME/.config" XDG_CACHE_HOME="$GUI_HOME/.cache" XDG_DATA_HOME="$GUI_HOME/.local/share" XDG_STATE_HOME="$GUI_HOME/.local/state" \
  PATH="$GUI_DIR/shim:${GUI_PATH_PREFIX:+$GUI_PATH_PREFIX:}/usr/bin:/bin" LANG=en_US.UTF-8 \
  GUI_DIR="$GUI_DIR" GUI_WORK="$GUI_WORK" \
  RDO_SHIM_LOG="${RDO_SHIM_LOG:-$GUI_WORK/run/open-shim.log}" \
  RDO_NODE="${RDO_NODE:-/opt/homebrew/bin/node}" \
  RDO_CHROMIUM="${RDO_CHROMIUM:?}" RDO_PWCORE="${RDO_PWCORE:?}" \
  RDO_BROWSER_UD="${RDO_BROWSER_UD:-$GUI_WORK/browserprof/ud}" \
  RDO_BROWSER_OUT="${RDO_BROWSER_OUT:-$GUI_WORK/run/browser-out}" \
  RDO_SIDECAR="${RDO_SIDECAR:-}" \
  ${GUI_EXTRA:-} \
  "$@"
