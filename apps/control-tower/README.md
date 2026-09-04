> ### Reference application for [Context Runtime](https://github.com/redevops-io/context-runtime)
>
> A focused AI system for **business intelligence**. Context Runtime ships a tenant that learns **which Metabase query set to run per question** — in its offline benchmark the learned policy scores **5.326 vs 1.643** against a core-query-set baseline ([`examples/control_tower.py`](https://github.com/redevops-io/context-runtime/blob/main/examples/control_tower.py)).
>
> ```
> Context Runtime  →  ReDevOps RAG  →  Sidekick  →  Application logic
> ```
> One of the [ReDevOps](https://github.com/redevops-io) reference applications built on Context Runtime.

---

# agentic-control-tower — agent layer + dashboard over a real Metabase core

The owner's single pane, built on the same "agentic module on a real OSS core" pattern
as [`apps/billing`](../billing) — but pointed at a different core. Instead of Lago's
REST collections, Control Tower wraps a running self-hosted **Metabase** instance (the
open-source BI core) and runs REAL analytical queries via `POST /api/dataset`:

- an **agent layer** that runs REAL Metabase queries (native SQL) over the REST API, and
- an **MD3 dashboard** rendered from those live query results (no mock data) — the
  control-tower layout from `deploy/module_service.py`: an "Ask anything" pill bar, KPI
  scorecards with inline-SVG sparklines, a revenue/trend bar chart, and a breakdown table,

for the demo tenant **Meridian Wealth Management**

```
Metabase (OSS BI core, :3001) ──POST /api/dataset──▶ app.py (FastAPI, :8202)
        ▲                                              ──▶ MD3 dashboard + /api/activity + /agent/run
        └── seed.py bootstraps admin + session token, ensures the Sample DB,            (ask, refresh)
            and creates the "Revenue by month" / "Revenue by category" saved cards
```

## Files

| File | Purpose |
|------|---------|
| `seed.py` | Bootstraps Metabase first-run (setup token → admin user → session token; falls back to login on re-runs), ensures the bundled **Sample Database** is synced, creates 2 saved cards, and writes `.env`. stdlib-only, idempotent. |
| `app.py` | FastAPI service (port 8202): `/health`, `/api/activity`, `/` dashboard, `/agent/run`. Runs real native-SQL queries via `/api/dataset`. |
| `requirements.txt` | fastapi, uvicorn, httpx. |
| `Dockerfile` | slim-python image running `uvicorn app:app --port 8202`. |
| `.env` | Written by `seed.py`: `METABASE_API_URL`, `METABASE_SESSION`, `METABASE_DB_ID`, `METABASE_FRONT_URL`, `CONTROL_TOWER_AS_OF`. |

## Metabase bootstrap method (the one that worked)

A fresh Metabase (H2 embedded) needs a first-run admin before its API is usable. The
reliable bootstrap is **the setup API**, done by `seed.py`:

1. `GET /api/session/properties` → read the one-time **`setup-token`**.
2. `POST /api/setup` with `{token, user, prefs, database:null}` → creates the admin user
   `admin@meridianwealth.test` (password `$METABASE_ADMIN_PASSWORD`) and returns a **session id**
   (the `X-Metabase-Session` token used for every later call).
3. **Idempotency:** once a user exists, `/api/setup` returns `403 ("can only be used to
   create the first user")` — so `seed.py` falls back to `POST /api/session` (login with
   the known creds) to get a fresh session token. Safe to re-run any time.

Notes discovered against this running instance (Metabase **v0.62.2.7**):

- The bundled **Sample Database** ships as `id 1` (engine `h2`) — `is_sample: true`. It's
  the deliberate, reliable data source: the Lago billing DB (`lago-db`) lives on a
  different docker network and is not reachable, so no external Postgres source is added.
- Queries run through **`POST /api/dataset`** with `{"database":1,"type":"native",
  "native":{"query": "<SQL>"}}`; results come back under `data.cols` / `data.rows`.
- **`month` is a reserved word** in this H2 build — alias the grouped column something
  else (the queries use `ym`), or you get a `42001` syntax error.
- The Sample `ORDERS` data spans **Apr 2025 → Apr 2029**, so the dashboard anchors to a
  fixed **as-of month** (`CONTROL_TOWER_AS_OF=2026-06-30`, matching the demo date) for a
  stable, full-data window.

## Seed + run

```bash
cd apps/control-tower

# 1. Bootstrap Metabase + create cards (idempotent — writes .env with the session token)
python3 seed.py
#   → SEED_OK core=metabase db_id=1 months=6 latest_month=2026-06 latest_revenue=15518 cards=2

# 2. Install deps + run the service
pip install -r requirements.txt          # add --break-system-packages on PEP-668 hosts
python3 -m uvicorn app:app --host 0.0.0.0 --port 8202
#   app.py auto-loads .env, so METABASE_SESSION is picked up with no manual copy.

# Or with Docker (point METABASE_API_URL at the Metabase service, not localhost):
docker build -t agentic-control-tower .
docker run --rm -p 8202:8202 \
  -e METABASE_API_URL=http://host.docker.internal:3001 \
  -e METABASE_SESSION=<token from .env> \
  -e METABASE_DB_ID=1 \
  -e METABASE_FRONT_URL=http://localhost:3001 \
  agentic-control-tower
```

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `METABASE_API_URL` | `http://localhost:3001` | Metabase REST base (`/api/...`). |
| `METABASE_SESSION` | _(from .env)_ | `X-Metabase-Session` token from the seed. |
| `METABASE_DB_ID` | `1` | Database id to query (Sample Database). |
| `METABASE_FRONT_URL` | `http://localhost:3001` | Metabase UI link for the "Open in Metabase ↗" button. |
| `CONTROL_TOWER_AS_OF` | `2026-06-30 23:59:59` | As-of cutoff so the demo window is stable. |
| `PORT` | `8202` | uvicorn bind port. |
| `ANTHROPIC_API_KEY` | _(optional)_ | If set, `/agent/run` "ask" can use Claude (`claude-opus-4-8`) to pick the best report template and add a one-line reasoning blurb. The endpoint works fully without it — routing is deterministic keyword matching, and only PRE-WRITTEN SQL is ever executed (the LLM never authors SQL). |

## Endpoints

- `GET /health` → `{"status":"ok","core":"metabase","connected": <bool from GET /api/health>}`
- `GET /api/activity` → KPI scorecards (revenue this month, orders, 6-mo revenue, AOV — each
  with a sparkline series), a revenue trend series, and a category breakdown — all from live
  `/api/dataset` queries. Cached 15s.
- `GET /` → the MD3 control-tower dashboard rendered from the live data. Header shows
  "Meridian Wealth Management", a green "agent active · core: Metabase connected" pill, a
  "data: live from Metabase" badge, and an **"Open in Metabase ↗"** button.
- `POST /agent/run` with `{"action": ...}`:
  - `"ask"` + `"question"` → maps the natural-language question to a SQL template
    (deterministic; LLM-assisted if a key is set), runs it via `/api/dataset`, and returns
    a plain-language `answer` + the real `columns`/`rows`.
  - `"refresh"` → re-runs the dashboard queries (busts the cache) and returns fresh KPIs.
  - **No approval gate:** Control Tower is read-only analytics — nothing moves money or
    mutates the core — so unlike billing's `refund`, every response states
    `approval: "not required — read-only analytics (no destructive action)"`.
