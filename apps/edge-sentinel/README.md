> ### Reference application for [Context Runtime](https://github.com/redevops-io/context-runtime)
>
> A focused AI system for **security operations**. Context Runtime ships a tenant that learns **which sources to pull per alert (CrowdSec · threat-intel · EDR)** — in its offline benchmark the learned policy scores **0.900 vs 0.800** against a always-full baseline ([`examples/soc_triage.py`](https://github.com/redevops-io/context-runtime/blob/main/examples/soc_triage.py)).
>
> ```
> Context Runtime  →  ReDevOps RAG  →  Sidekick  →  Application logic
> ```
> One of the [ReDevOps](https://github.com/redevops-io) reference applications built on Context Runtime.

---

# edge-sentinel — agent layer + SOC dashboard over a real CrowdSec core

A sibling of [`apps/billing`](../billing) (the reference vertical slice), same pattern,
new core. It wraps the running self-hosted **CrowdSec** instance (the open-source
detection & decision engine) with:

- an **agent layer** that reads REAL CrowdSec data — live decisions over the LAPI bouncer
  API + alerts via `cscli ... -o json`, and
- an **MD3 SOC dashboard** rendered from that live data (no mock data),

for the demo tenant **Meridian Wealth Management** (a wealth-management firm).

```
CrowdSec (OSS core, LAPI :8086) ──GET /v1/decisions (X-Api-Key)──┐
                                ──cscli alerts list -o json───────┤
                                                                  ▼
                                       app.py (FastAPI, :8203) ──▶ MD3 SOC dashboard
                                                                   + /api/activity
                                                                   + /agent/run
        ▲                                          agentic actions: block_ip (approval-gated)
        └── seed.py: cscli bouncers add + cscli decisions add (idempotent)   approve_block · triage
```

## Files

| File | Purpose |
|------|---------|
| `seed.py` | Bootstrap + seed via `cscli` (docker exec): adds the `meridian-agent` bouncer, captures its API key, injects 6 varied decisions/alerts, writes `.env`. Idempotent. |
| `app.py` | FastAPI service (port 8203): `/health`, `/api/activity`, `/` SOC dashboard, `/agent/run`. |
| `requirements.txt` | fastapi, uvicorn, httpx. |
| `Dockerfile` | slim-python image running `uvicorn app:app --port 8203`. |
| `.env` | Written by `seed.py`: `CROWDSEC_LAPI_URL`, `CROWDSEC_BOUNCER_KEY`, `CROWDSEC_CONTAINER`, `CROWDSEC_FRONT_URL`. |

## CrowdSec bootstrap method (the one that worked)

CrowdSec has **no rich web UI** and is **CLI-managed** via `cscli`. There are two read
paths, both driven from the running container `agentic-cores-crowdsec-1`:

1. **Decisions** — the bouncer/LAPI path. Create a bouncer credential and read live
   decisions over the Local API:
   ```bash
   KEY=$(sudo docker exec agentic-cores-crowdsec-1 cscli bouncers add meridian-agent -o raw)
   curl -s -H "X-Api-Key: $KEY" http://localhost:8086/v1/decisions
   ```
2. **Alerts** — LAPI alert auth needs a machine login, so the simplest reliable path is
   to shell out to `cscli` (which is already authenticated inside the container):
   ```bash
   sudo docker exec agentic-cores-crowdsec-1 cscli alerts list -o json
   ```

`seed.py` automates step 1 (and re-creates the bouncer if it already exists so the key is
always usable), injects the seed activity, and writes `.env`.

Key facts for CrowdSec **v1.7.8** (discovered on this host):

- The container is **`agentic-cores-crowdsec-1`**; LAPI is on **http://localhost:8086**.
- `cscli decisions add` **also creates a matching alert**, so seeding decisions populates
  both `/v1/decisions` and `cscli alerts list`.
- LAPI has no public `/health`; a TCP+HTTP response (even `404` on `/`) means the service
  is up — `crowdsec_connected()` treats any `< 500` as connected.
- A decision's `duration` counts down (e.g. `3h59m59s`); `scope` is `Ip` or `Range`.

## Seed + run

```bash
cd apps/edge-sentinel

# 1. Bootstrap + seed CrowdSec (idempotent — writes .env with the live bouncer key)
python3 seed.py
#   → BOUNCER_KEY=<key>
#   → SEED_OK bouncer=meridian-agent added=6 decisions=6 alerts=6
#   → Wrote .env

# 2. Install deps + run the service
pip install -r requirements.txt          # add --break-system-packages on PEP-668 hosts
python3 -m uvicorn app:app --host 0.0.0.0 --port 8203
#   app.py auto-loads .env, so CROWDSEC_BOUNCER_KEY is picked up with no manual copy.

# Or with Docker (point the LAPI URL at the CrowdSec service; mount the docker socket
# for the cscli-backed alert/triage/approve paths):
docker build -t edge-sentinel .
docker run --rm -p 8203:8203 \
  -e CROWDSEC_LAPI_URL=http://host.docker.internal:8086 \
  -e CROWDSEC_BOUNCER_KEY=<key from .env> \
  -v /var/run/docker.sock:/var/run/docker.sock \
  edge-sentinel
```

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `CROWDSEC_LAPI_URL` | `http://localhost:8086` | CrowdSec Local API base (`/v1/decisions`). |
| `CROWDSEC_BOUNCER_KEY` | _(from .env)_ | `X-Api-Key` for reading decisions — the key from `cscli bouncers add`. |
| `CROWDSEC_CONTAINER` | `agentic-cores-crowdsec-1` | Container name for `cscli` (alerts, ban enforcement). |
| `CROWDSEC_FRONT_URL` | `http://localhost:8086` | Link for the "Open CrowdSec metrics ↗" button (CrowdSec is CLI-managed; this is the LAPI endpoint). |
| `PORT` | `8203` | uvicorn bind port. |
| `ANTHROPIC_API_KEY` | _(optional)_ | If set, `triage` adds an LLM reasoning blurb (model `claude-opus-4-8`). The endpoint works fully without it — actions are deterministic CrowdSec/cscli calls. |

## Endpoints

- `GET /health` → `{"status":"ok","core":"crowdsec","connected": <bool>}`
- `GET /api/activity` → live KPIs (threats blocked, alerts 24h, unique source IPs, last
  event) + active decisions + alert feed + top scenarios, all from CrowdSec. Cached 10s.
- `GET /` → the MD3 SOC dashboard from the live data: a **status banner** first (green
  "All systems normal" / red when there are active threats), **KPI tiles**, an
  **alert feed grouped by severity** with color pills, a **scenario bar meter**, and an
  **attack-sources / active-decisions table**. Header shows "Meridian Wealth Management", a green
  "agent active · core: CrowdSec connected" pill, and an "Open CrowdSec metrics ↗" link.
- `POST /agent/run` with `{"action": ...}`:
  - `"block_ip"` `{ip}` → the **approval-gated** sensitive action. Returns
    `{"status":"pending_approval","summary":"block <ip>"}` — never auto-bans.
  - `"approve_block"` `{ip, duration?, reason?}` → the approved path: actually runs
    `cscli decisions add` to enforce the ban, then refreshes the dashboard cache.
  - `"triage"` → summarizes active decisions + alerts (severity mix, top scenarios);
    adds a one-line LLM reasoning blurb iff `ANTHROPIC_API_KEY` is set.
