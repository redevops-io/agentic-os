#!/bin/sh
# Snap service wrapper for the ReDevOps Projects control plane (strict confinement).
# Reads user config set with `snap set redevops-projects …` and maps it onto the runtime's
# model env. Both LLM setups are supported:
#   Hosted:  snap set redevops-projects llm.api-key=sk-...   [llm.base-url=… llm.model=…]
#   Local:   snap set redevops-projects llm.base-url=http://localhost:11434/v1   (no key)
set -eu

BASE="$(snapctl get llm.base-url 2>/dev/null || true)"
[ -n "$BASE" ] || BASE="https://api.openai.com/v1"     # default so "just set a key" works
PORT="$(snapctl get port 2>/dev/null || true)"
[ -n "$PORT" ] || PORT="8787"

export SIDEKICK_MODEL_BASE_URL="$BASE"
export SIDEKICK_MODEL_API_KEY="$(snapctl get llm.api-key 2>/dev/null || true)"   # empty = local/no-key
export SIDEKICK_MODEL_NAME="$(snapctl get llm.model 2>/dev/null || true)"

# Strict confinement + a local try: bind loopback only. Data/config live under the snap's
# per-user common dir (writable under confinement).
export PROJECTS_HOST="127.0.0.1"
export PROJECTS_PORT="$PORT"
export OUTCOME_STORE_PATH="${SNAP_USER_COMMON:-$HOME}/outcomes.jsonl"

echo "ReDevOps Projects (snap) → http://127.0.0.1:${PORT}"
if [ -n "$SIDEKICK_MODEL_API_KEY" ]; then echo "  LLM: hosted ($BASE)"
elif [ -n "$BASE" ]; then echo "  LLM: $BASE"; fi

exec "$SNAP/bin/agentic-os-projects"
