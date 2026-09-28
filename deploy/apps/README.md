# ReDevOps Business Apps — portable deployment

The four-business-systems agentic suite, packaged to run on one Docker host
(self-host or a cloud VM). This is the same stack that runs at
`demo.redevops.io/demo`, with the proxmox-specific bits (cloudflared, host
networking, the local registry) removed and every image pulled from GHCR.

## What deploys

| Layer | Services | Registry |
|---|---|---|
| **Operating console** | `projects-console` (the `demo.redevops.io/demo` surface) | public (GHCR) |
| **6 governed domain agents** | revenue · intelligence · finance · customer-success · content · security-compliance | public (GHCR) |
| **OSS cores** | Twenty CRM · Metabase · Postiz (+ their Postgres/Redis) | public (Docker Hub) |
| **Front door** | Caddy | public |

The domain agents include the **learning + discovery** engine. All images are
public — no registry login. The runtimes + control plane underneath install
separately (see `/projects/core-install`).

## Prerequisites

- A host with **>= 32 GB RAM** and **Docker + Docker Compose v2** (Twenty +
  Metabase + Postiz + four Postgres instances are heavy).
- An **OpenAI-compatible LLM endpoint** the host can reach (your own vLLM/Ollama,
  or a hosted provider).

## Deploy

```bash
git clone https://github.com/redevops-io/agentic-os.git
cd agentic-os/deploy/apps

cp .env.example .env
# edit .env: set TWENTY_APP_SECRET and POSTIZ_JWT_SECRET (openssl rand -base64 32),
# point REDEVOPS_LLM_BASE_URL at your model, set TENANT.

docker compose up -d                 # public images — no login needed

# watch it come up (first boot pulls images + runs DB migrations)
docker compose ps
```

Open **http://\<host\>:8080** — the operating console, with a tile per business
system and a cross-domain attention queue.

## First-run wiring

- **Twenty** (`:3010`): create an account, then a personal **API key**
  (Settings → APIs). Paste it into `.env` as `TWENTY_API_KEY` and
  `docker compose up -d revenue-agent` to connect the revenue agent.
- **Metabase** (`:3001`) and **Postiz** (`:4200`): complete their first-run setup;
  the intelligence / finance / content agents connect by service name automatically.

Agents **degrade gracefully** — a core that isn't wired yet just shows
`connected: false` on that agent's `/health`; nothing crashes.

## Verify

```bash
for p in 8220 8221 8222 8230 8231 8232; do
  curl -fsS http://localhost:$p/health && echo || echo "agent on $p not ready yet"
done
curl -fsS http://localhost:8080/health && echo   # console
```

## Safety

`SHADOW_MODE=true` (the default) keeps every agent in **stage-for-approval**
mode: it drafts the action and a human approves it in the console — nothing is
sent to a real CRM / social account / customer until you approve. Flip to
`false` only once you've reviewed the behavior for your tenant.

## Teardown

```bash
docker compose down           # stop, keep data volumes
docker compose down -v        # stop and delete all data
```
