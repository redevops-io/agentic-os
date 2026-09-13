#!/bin/sh
# The desktop "app" action: open the running control plane in the user's browser.
# The `projects` daemon auto-starts on install, so it should already be serving.
set -eu
PORT="$(snapctl get port 2>/dev/null || true)"
[ -n "$PORT" ] || PORT="8787"
exec xdg-open "http://127.0.0.1:${PORT}"
