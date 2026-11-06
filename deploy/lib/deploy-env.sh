#!/usr/bin/env bash
# Shared by the deploy/*/deploy.sh scripts: where a deployment goes is configuration, never source.
#
#   deploy_load_env              sources deploy/deploy.local.env when it exists (gitignored; start from deploy.local.env.example)
#   deploy_require NAME "what"   stops with a clear message when NAME is empty or still the REPLACE_ME marker
#
# Real values (host addresses, directories on the target) live in the environment or in deploy/deploy.local.env.
# See docs/DEPLOYMENT_ADDRESSES.md.

deploy_load_env() {
  local dir
  dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [ -f "$dir/deploy.local.env" ]; then
    # shellcheck disable=SC1091
    . "$dir/deploy.local.env"
  fi
}

deploy_require() {
  local name="$1" what="$2"
  if [ -z "${!name:-}" ] || [ "${!name}" = "REPLACE_ME" ]; then
    echo "error: $name is not set: $what" >&2
    echo "       export $name=<value>, or put it in deploy/deploy.local.env (copy deploy/deploy.local.env.example; the file is gitignored)." >&2
    exit 2
  fi
}
