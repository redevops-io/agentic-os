> ### Reference application for [Context Runtime](https://github.com/redevops-io/context-runtime)
>
> A focused AI system for **social media**. Context Runtime ships a tenant that learns **which channel/timing/content strategy to run per goal** — in its offline benchmark the learned policy scores **3.875 vs 0.773** against a fixed-strategy baseline ([`examples/social_autopilot.py`](https://github.com/redevops-io/context-runtime/blob/main/examples/social_autopilot.py)).
>
> ```
> Context Runtime  →  ReDevOps RAG  →  Sidekick  →  Application logic
> ```
> One of the [ReDevOps](https://github.com/redevops-io) reference applications built on Context Runtime.

---

# agentic-social-autopilot — agent layer + dashboard over a real Postiz core

Follows the **agentic-billing** reference pattern, applied to social media. It wraps the
running self-hosted **Postiz** instance (the open-source social-scheduling core) with:

- an **agent layer** that reads REAL Postiz data (scheduled posts, channels, followers), and
- an **MD3 dashboard** rendered from that live data (no mock data),

for the demo tenant **Meridian Wealth Management** (a wealth-management firm).

```
Postiz (OSS core) ──▶ app.py (FastAPI, :8206) ──▶ MD3 dashboard + /api/activity + /agent/run
   postgres ▲                                       agentic actions (draft, publish [approval-gated])
            └── seed.py bootstraps org + user + channels + 6 posts (idempotent)
```

## Files

| File | Purpose |
|------|---------|
| `seed.py` | Idempotent seeder. Hashes the user password with the container's own bcrypt, then inserts org + user + 3 channels + 6 posts into the Postiz postgres. Writes `.env`. |
| `app.py` | FastAPI service (port 8206): `/health`, `/api/activity`, `/` dashboard, `/agent/run`. |
| `requirements.txt` | fastapi, uvicorn, httpx. |
| `Dockerfile` | slim-python image running `uvicorn app:app --port 8206`. |
| `.env` | Written by `seed.py`: `POSTIZ_FRONT_URL`, `POSTIZ_PG_*`, `POSTIZ_ORG_ID`, `POSTIZ_API_KEY`. |

## Postiz bootstrap method — what actually worked (be honest: it's the DB)

I investigated the REST API first, as instructed. **The HTTP API is unreachable in this
stack**, so the agent reads the Postiz **postgres directly**. Details:

- Postiz here is `ghcr.io/gitroomhq/postiz-app:latest`, one container
  (`agentic-postiz-postiz-1`) running **frontend** (Next.js, :4200), **backend** (NestJS),
  and **orchestrator** (:3002) under pm2, fronted by **nginx on :5000** (published as
  host **:4200**). nginx proxies `location /api/ -> http://localhost:3000/`.
- The backend's routes (`/auth/register`, `/auth/login`, `/public/v1/posts`, …) all map
  correctly at startup, **but the backend never binds port 3000**: on `onModuleInit` it
  tries to connect to a **Temporal** server at `127.0.0.1:7233` (`@temporalio/worker`),
  which **is not deployed** in this stack. That throws, `app.listen()` never completes,
  nothing listens on 3000, and every `/api/...` call returns **502 Bad Gateway** (nginx →
  connection refused). Confirmed in `backend-error.log`:
  `No connection established. Last error: connect ECONNREFUSED ::1:7233`.
- So `POST /api/auth/register` / `/login` and the public API (`/public/v1/posts` with an
  API key) **cannot be reached** here. They would work if a Temporal server (port 7233)
  were added to the compose stack — at which point you can point `POSTIZ_API_URL` at it
  and add a REST `fetch_*` with no change to the dashboard.

**The path that works (and that the task explicitly authorises): seed + read the Postiz
postgres directly** (`agentic-postiz-postiz-postgres-1`, db/user/pass `postiz`). Schema
facts discovered by inspecting the running DB (Prisma-managed):

- Tables are PascalCase + quoted: `"Organization"`, `"User"`, `"UserOrganization"`,
  `"Integration"`, `"Post"`.
- `"User".password` is **bcrypt cost 10** (`AuthService.hashPassword` → `bcrypt.hashSync(pw,10)`).
  `seed.py` generates the hash with the container's own bcrypt so the seeded user is a
  **real, loginable account** (once a Temporal server makes the UI/login usable).
- A post's state lives in `"Post".state` enum: `QUEUE` (scheduled), `DRAFT`, `PUBLISHED`,
  `ERROR`. `"Post".content` is a JSON string of the editor value-array: `[{"content":"…"}]`.
- A `"Post"` requires an `"Integration"` (the channel) and an `"Organization"`. Channels
  carry follower counts in their `profile` JSON (we model them there).
- `"User".audience` holds the headline follower number.

## What gets seeded

- **Org** `Meridian Wealth Management` (stable `apiKey`).
- **User** `agent@meridianwealth.com` / `$POSTIZ_USER_PASSWORD` (bcrypt, `SUPERADMIN`).
- **3 channels** (Integrations): Instagram (3,120), Facebook (4,850), Google Business
  (980 followers). No real social account is linked — they're modelled channels with
  placeholder tokens, exactly as the task allows.
- **6 posts**: the wealth-management content calendar —
  1. Instagram · *Q3 market commentary* (scheduled)
  2. Facebook · *5 year-end planning questions* (scheduled)
  3. Facebook · *Tax-loss harvesting reminder* (scheduled)
  4. Instagram · *Q4 outlook carousel* (draft)
  5. Google Business · *Retirement-planning webinar invite* (scheduled)
  6. Instagram · *Meet your advisor* (published — so the feed shows a live item)

## Seed + run

```bash
cd apps/social-autopilot

# 1. Seed Postiz postgres (idempotent — safe to re-run; writes .env)
python3 seed.py
#   → SEED_OK org=meridian-wealth-org user=agent@meridianwealth.com integrations=3 posts=6

# 2. Install deps + run the service
pip install -r requirements.txt          # add --break-system-packages on PEP-668 hosts
python3 -m uvicorn app:app --host 0.0.0.0 --port 8206
#   app.py auto-loads .env.
```

> The agent reads Postiz's postgres via `sudo docker exec <pg-container> psql …`, so run
> `app.py` on the host (where the docker socket is). The Docker image is provided for
> parity; to run it containerised, mount the docker socket or switch to the REST path
> once a Postiz with a live API is available.

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `POSTIZ_FRONT_URL` | `http://localhost:4200` | Postiz UI link for the "Open in Postiz ↗" button. |
| `POSTIZ_PG_CONTAINER` | `agentic-postiz-postiz-postgres-1` | postgres container the agent reads. |
| `POSTIZ_PG_USER` / `POSTIZ_PG_DB` | `postiz` / `postiz` | postgres creds. |
| `POSTIZ_ORG_ID` | `meridian-wealth-org` | org id reads are scoped to. |
| `POSTIZ_API_URL` | `http://localhost:4200` | REST base — used only for the connectivity probe / future API reads. |
| `PORT` | `8206` | uvicorn bind port. |
| `ANTHROPIC_API_KEY` | _(optional)_ | If set, `/agent/run` `"draft"` writes copy with Claude (`claude-opus-4-8`); a deterministic template is always the fallback. |

## Endpoints

- `GET /health` → `{"status":"ok","core":"postiz","connected": <bool>}` (true iff the
  Postiz postgres is readable).
- `GET /api/activity` → live KPIs (scheduled posts, engagement 7d, followers, reach 7d) +
  the publishing queue (next 7 days) + per-network stats, all derived from Postiz postgres.
  Engagement/reach/new-followers are deterministic functions of the **real** follower
  counts (so they track the seeded data rather than being hard-coded). Cached 15s.
- `GET /` → the MD3 social dashboard (Hootsuite/Buffer style: KPI tiles, a next-7-days
  publishing queue feed, an engagement-by-network bar list, a per-network stats table).
  Header shows "Meridian Wealth Management", a green "agent active · core: Postiz connected" pill,
  a "data: live from Postiz" badge, and an **"Open in Postiz ↗"** button. An approval
  banner shows the next post the agent could publish.
- `POST /agent/run` with `{"action": ...}`:
  - `"draft"` `{topic}` → generates post copy (LLM if `ANTHROPIC_API_KEY` is set, else a
    template) and **stages it as a real `DRAFT`** row in Postiz. Never publishes.
  - `"publish"` `{id}` → **approval-gated** (module declares `approval_required:["publish"]`).
    Returns `{"status":"pending_approval", ...}` and performs **no** write/publish.
