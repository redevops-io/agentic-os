# Try ReDevOps — a sandboxed Projects control plane

The lowest-friction way to try ReDevOps on **Windows, macOS or Linux** before deploying the whole
thing. It runs the **Projects control plane** (connect apps, run governed missions, permissions UI) in
a throwaway container. Nothing touches your host; delete the container and it's gone.

> This is the *try-before-you-deploy* sandbox. Deploying the full stack — Sidekick driving
> Terraform/kubectl, the Prometheus+Grafana+Loki combo — is the [Docker-Compose / Helm quickstart](https://redevops.io/projects/install),
> which orchestrates host infrastructure and can't run inside this sandbox.

## Bring your own LLM — hosted or local

You provide the model on setup; both are first-class:

| Setup | What you set |
|---|---|
| **Hosted / cloud** | `LLM_API_KEY` (+ optional `LLM_BASE_URL`, `LLM_MODEL`) — defaults to OpenAI |
| **Local / self-hosted** | `LLM_BASE_URL` pointing at a model on your machine (Ollama/vLLM/LM Studio); no key |

## One command

**Hosted (bring your key):**
```bash
docker run --rm -p 8787:8787 -e LLM_API_KEY=sk-... \
  ghcr.io/redevops-io/redevops-projects-try:latest
# → open http://localhost:8787
```

**Local model (Ollama on your machine, no key):**
```bash
docker run --rm -p 8787:8787 \
  -e LLM_BASE_URL=http://host.docker.internal:11434/v1 -e LLM_MODEL=llama3.1 \
  --add-host=host.docker.internal:host-gateway \
  ghcr.io/redevops-io/redevops-projects-try:latest
```
(`--add-host` is only needed on Linux; Docker Desktop on Windows/macOS provides `host.docker.internal`.)

**Or with Compose (edit `.env` first):**
```bash
cp deploy/try/.env.example deploy/try/.env    # fill in your LLM setup
docker compose -f deploy/try/docker-compose.try.yml up
```

## Build it yourself

The published image is produced from this repo. To build locally (from the repo root):
```bash
docker build -f deploy/try/Dockerfile -t redevops-projects-try .
docker run --rm -p 8787:8787 -e LLM_API_KEY=sk-... redevops-projects-try
```

## Notes
- Bound to `127.0.0.1` — it's a local try, not a public server. To expose it, publish on `0.0.0.0`
  **and** set `AGENTIC_OS_API_KEY` to gate the write API.
- The container can't bundle a multi-GB model — for a zero-cloud try, run a local model (Option B).
- Ubuntu users can instead install the native **snap** (`snap install redevops-projects`), which is the
  same control plane under strict confinement — see [`snap/`](../../snap).
