> ### Reference application for [Context Runtime](https://github.com/redevops-io/context-runtime)
>
> A focused AI system for **growth & attribution**. Context Runtime ships a tenant that learns **which attribution window and source bundle to use per lead-source query** — in its offline benchmark the learned policy scores **7.851 vs 5.282** against a fixed-window baseline ([`examples/growth_engine.py`](https://github.com/redevops-io/context-runtime/blob/main/examples/growth_engine.py)).
>
> ```
> Context Runtime  →  ReDevOps RAG  →  Sidekick  →  Application logic
> ```
> One of the [ReDevOps](https://github.com/redevops-io) reference applications built on Context Runtime.

---

# agentic-growth-engine — agent layer + dashboard over a real Umami core

An "agentic module on a real OSS core" vertical slice (same pattern as the
[`agentic-billing`](../billing) reference, which wraps Lago). This one wraps the running
self-hosted **Umami** instance (the open-source web-analytics core) with:

- an **agent layer** that reads REAL Umami data over its REST API (stats + UTM / referrer
  metrics) and turns it into **lead-source attribution** + growth KPIs, and
- a GA4 / HubSpot-style **MD3 dashboard** rendered from that live data (no mock data),

for the demo tenant **Meridian Wealth Management** (a wealth-management / RIA firm).

```
Umami (OSS core, :3002) ──REST──▶ app.py (FastAPI, :8205) ──▶ MD3 dashboard + /api/activity + /agent/run
        ▲                                                       agentic actions (analyze, reallocate_budget)
        └── seed.py bootstraps the website + inserts dated session/event rows (idempotent)
```

## Files

| File | Purpose |
|------|---------|
| `seed.py` | Idempotent seeder: logs in, creates/reuses the **Meridian Wealth** website, then inserts REAL `session` + `website_event` rows (dated across the last 8 days, with `utm_source`/`utm_medium`/`utm_campaign`/`referrer_domain`) directly into the Umami Postgres, and fires a couple of `/api/send` events. Writes `.env`. |
| `app.py` | FastAPI service (port **8205**): `/health`, `/api/activity`, `/` dashboard, `/agent/run`. |
| `requirements.txt` | fastapi, uvicorn, httpx. |
| `Dockerfile` | slim-python image running `uvicorn app:app --port 8205`. |
| `.env` | Written by `seed.py`: `UMAMI_URL`, `WEBSITE_ID`, `UMAMI_ADMIN_*`, `UMAMI_FRONT_URL`. |

## Umami bootstrap method (the one that worked)

Umami v2's REST API is ready out of the box; you just need an auth token + a website id:

1. **Login** — `POST /api/auth/login {"username":"admin","password":"umami"}` → `{ "token": ... }`.
   Use it as `Authorization: Bearer <token>`. (`app.py` re-logs in automatically and caches
   the token for 10 minutes — no manual key handling.)
2. **Create the website** — `POST /api/websites {"name":"Meridian Wealth","domain":"meridianwealth.test"}`
   → a website object whose **`id`** (a UUID) is captured and written to `.env` as `WEBSITE_ID`.
   `seed.py` looks the site up by name first, so re-runs reuse the same id (idempotent).

### Seeding: REAL analytics rows over RECENT days

Umami's public ingest, `POST /api/send`, stamps every event with `created_at = now()`, so it
**cannot** place pageviews across the *past* week — which lead-source attribution needs. So
`seed.py` inserts the rows directly into the Umami Postgres (container
**`agentic-cores-umami-db-1`**, db/user/pass = `umami`), using the exact columns the Umami app
itself writes:

- `session` rows (browser / os / device / screen / city, dated `created_at`), and
- `website_event` rows with `url_path`, `page_title`, `referrer_domain`, and dedicated
  **`utm_source` / `utm_medium` / `utm_campaign`** columns, dated across the last 8 days.

Because these are the same columns Umami writes, every Umami REST report reads them back as
genuine traffic. A couple of `/api/send` calls are also fired so the public ingest path is
exercised end-to-end. The seed is deterministic (fixed RNG) and clears prior rows for the site
first, so it's safe to re-run.

Seeded mix (~60 pageviews / 37 sessions across 4 wealth-management lead channels):

| Channel | utm_source | utm_medium | referrer |
|---------|-----------|-----------|----------|
| Google Ads | `google` | `cpc` | google.com |
| LinkedIn | `linkedin` | `cpc` | linkedin.com |
| Seminar QR | `seminar-qr` | `referral` | seminar-qr.test |
| Organic / Direct | — | `organic` | (none) |

### Reading attribution back (Umami metric types)

The Umami v2 metric `type` keys are **camelCase**: `referrer`, `utmSource`, `utmMedium`,
`utmCampaign` (plus `url`, `query`, etc.). `app.py` reads `utmSource`/`utmMedium`/`utmCampaign`
+ `referrer` and falls back to `referrer` if UTM is empty.

```bash
TOKEN=$(curl -s -X POST http://localhost:3002/api/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"umami"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
WID=<website id from .env>
RANGE="startAt=$(( ($(date +%s)-86400*30)*1000 ))&endAt=$(( ($(date +%s)+86400)*1000 ))"
curl -s "http://localhost:3002/api/websites/$WID/stats?$RANGE" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:3002/api/websites/$WID/metrics?type=utmSource&$RANGE" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:3002/api/websites/$WID/metrics?type=referrer&$RANGE"  -H "Authorization: Bearer $TOKEN"
```

## Seed + run

```bash
cd apps/growth-engine

# 1. Seed Umami (idempotent — safe to re-run; writes .env with the website id)
python3 seed.py
#   → SEED_OK website_id=<uuid> sessions=36 events=58 conversions=12
#       channels[Google Ads=11 LinkedIn=4 Organic / Direct=14 Seminar QR=7] public_ingest=ok

# 2. Install deps + run the service
pip install -r requirements.txt          # add --break-system-packages on PEP-668 hosts
python3 -m uvicorn app:app --host 0.0.0.0 --port 8205
#   app.py auto-loads .env (UMAMI_URL, WEBSITE_ID) — no manual config.

# Or with Docker (point UMAMI_URL at the Umami service, not localhost):
docker build -t agentic-growth-engine .
docker run --rm -p 8205:8205 \
  -e UMAMI_URL=http://host.docker.internal:3002 \
  -e WEBSITE_ID=<id from .env> \
  -e UMAMI_FRONT_URL=http://localhost:3002 \
  agentic-growth-engine
```

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `UMAMI_URL` | `http://localhost:3002` | Umami REST base (`/api/...`). |
| `WEBSITE_ID` | _(from .env)_ | The Meridian Wealth website id captured by the seed. |
| `UMAMI_ADMIN_USER` / `UMAMI_ADMIN_PASS` | `admin` / `umami` | Login used to mint a bearer token. |
| `UMAMI_FRONT_URL` | `http://localhost:3002` | Umami UI link for the "Open in Umami ↗" button (the human-operable path). |
| `PORT` | `8205` | uvicorn bind port. |
| `ANTHROPIC_API_KEY` | _(optional)_ | If set, `/agent/run` `analyze` adds an LLM reasoning blurb (model `claude-opus-4-8`). The endpoint works fully without it — the analysis is deterministic over real Umami data. |

## Endpoints

- `GET /health` → `{"status":"ok","core":"umami","connected": <bool from /api/heartbeat>}`
- `GET /api/activity` → live growth KPIs (blended CPL, leads, ROAS, booked rate) + lead-source
  attribution (real referrer / UTM), a conversion funnel, and a per-channel table with
  spend / CPL / ROAS — all derived from Umami REST. Cached 15s.
- `GET /` → the MD3 growth dashboard rendered from the live data: KPI tiles, a lead-source
  attribution bar breakdown, a lead-to-client conversion funnel, and a channel table with
  spend / CPL / ROAS columns. Header shows "Meridian Wealth Management", a green
  "agent active · core: Umami connected" pill, a "data: live from Umami" badge, and an
  **"Open in Umami ↗"** button (→ `http://localhost:3002`). An approval banner appears
  recommending a budget shift between paid channels.
- `POST /agent/run` with `{"action": ...}`:
  - `"analyze"` → summarizes which channels perform from the real attribution data (top
    channel, CPL, ROAS per channel) + a budget recommendation; adds an optional LLM blurb.
  - `"reallocate_budget"` `{from,to,amount}` → **approval-gated** (the module declares
    `approval_required:[budget_change]`). Returns `{"status":"pending_approval", ...}` and
    does **not** execute — actual ad spend lives in the external Ads platform (Google/Meta).
