#!/usr/bin/env sh
# Polished "Try ReDevOps" entrypoint — a sandboxed Projects control plane you can run on
# Windows / macOS / Linux via Docker Desktop before deploying the whole thing.
#
# You bring the LLM. Two setups, both supported:
#   • Cloud / hosted   — set LLM_API_KEY (+ optional LLM_BASE_URL, LLM_MODEL)
#   • Local / self-host — point LLM_BASE_URL at a model on your machine (no key needed)
#
# We map these friendly names onto the runtime's SIDEKICK_MODEL_* env, so a try user never
# has to learn the internal variable names.
set -eu

# ── LLM setup (hosted or local) ──────────────────────────────────────────────────────
# Defaults to OpenAI's endpoint so "just give me a key" works with nothing else set.
LLM_BASE_URL="${LLM_BASE_URL:-https://api.openai.com/v1}"
LLM_API_KEY="${LLM_API_KEY:-}"
LLM_MODEL="${LLM_MODEL:-}"

# host.docker.internal lets a container reach a model running on the HOST (Ollama, vLLM, LM Studio).
# Docker Desktop (Win/Mac) provides it automatically; on Linux add --add-host=host.docker.internal:host-gateway.

export SIDEKICK_MODEL_BASE_URL="$LLM_BASE_URL"
export SIDEKICK_MODEL_API_KEY="$LLM_API_KEY"
export SIDEKICK_MODEL_NAME="$LLM_MODEL"

# Serve on all interfaces INSIDE the container so the published port reaches your browser.
export PROJECTS_HOST="${PROJECTS_HOST:-0.0.0.0}"
export PROJECTS_PORT="${PROJECTS_PORT:-8787}"

# ── friendly banner ──────────────────────────────────────────────────────────────────
echo "──────────────────────────────────────────────────────────────"
echo " ReDevOps Projects — Try mode (sandboxed container)"
if [ -n "$LLM_API_KEY" ]; then
  echo " LLM: hosted   → $LLM_BASE_URL  (API key provided)"
elif printf '%s' "$LLM_BASE_URL" | grep -qiE 'host.docker.internal|localhost|127\.0\.0\.1|:11434|:8000'; then
  echo " LLM: local    → $LLM_BASE_URL  (no key — self-hosted model)"
else
  echo " LLM: NOT CONFIGURED — the UI will load, but Sidekick's model features stay off."
  echo "   Hosted:  docker run ... -e LLM_API_KEY=sk-...  redevops-projects-try"
  echo "   Local:   docker run ... -e LLM_BASE_URL=http://host.docker.internal:11434/v1  redevops-projects-try"
fi
echo " Open:  http://localhost:${PROJECTS_PORT}"
echo " Sandbox: nothing touches your host; remove the container and it's gone."
echo "──────────────────────────────────────────────────────────────"

exec agentic-os-projects
