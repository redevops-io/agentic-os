> ### Reference application for [Context Runtime](https://github.com/redevops-io/context-runtime)
>
> A focused AI system for **competitive intelligence**. Context Runtime ships a tenant that learns **which competitor watches to sweep per intel question** — in its offline benchmark the learned policy scores **3.611 vs 0.403** against a full-sweep baseline ([`examples/market_radar.py`](https://github.com/redevops-io/context-runtime/blob/main/examples/market_radar.py)).
>
> ```
> Context Runtime  →  ReDevOps RAG  →  Sidekick  →  Application logic
> ```
> One of the [ReDevOps](https://github.com/redevops-io) reference applications built on Context Runtime.

---

# agentic-market-radar — agent layer + dashboard over a real changedetection.io core

A competitive-intelligence module (Crayon/Klue style) for a roofing SME, built on the
**agentic-billing** reference pattern. It wraps the running self-hosted
**changedetection.io** instance (the open-source website-change-monitoring core) with:

- an **agent layer** that reads REAL watch data over its REST API, and
- an **MD3 dashboard** rendered from that live data (no mock data),

for the demo tenant **Summit Roofing Co.** (a roofing contractor). Every watch is a
competitor page, a price/pricing page, or a permits feed; the agent surfaces what
changed and can spin up new monitors.

```
changedetection.io (OSS core, :5001) ──REST /api/v1──▶ app.py (FastAPI, :8204) ──▶ MD3 dashboard + /api/activity + /agent/run
        ▲                                                         agentic actions (add_watch, brief)
        └── seed.py reads the api token from the datastore + creates the demo watches (idempotent)
```

## Files

| File | Purpose |
|------|---------|
| `seed.py` | Reads the api token from the container datastore, confirms via `GET /api/v1/systeminfo`, creates 5 roofing competitor/price/permit watches over REST (idempotent), writes `.env`. |
| `app.py` | FastAPI service (port 8204): `/health`, `/api/activity`, `/` dashboard, `/agent/run`. |
| `requirements.txt` | fastapi, uvicorn, httpx. |
| `Dockerfile` | slim-python image running `uvicorn app:app --port 8204`. |
| `.env` | Written by `seed.py`: `CD_API_URL`, `CD_API_KEY`, `CD_FRONT_URL`. |

## changedetection bootstrap method (the one that worked)

changedetection's REST API requires an api key in the `x-api-key` header. Unlike Lago,
**we do not create the key** — changedetection generates it on first boot and stores it
in its datastore. **Where to find it:**

```bash
sudo docker exec agentic-cores-changedetection-1 cat /datastore/changedetection.json \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["settings"]["application"]["api_access_token"])'
# -> <your-changedetection-api-token>   (unique per host)
```

(On this host the token lives at `settings.application.api_access_token` in
`/datastore/changedetection.json`; older builds use `/datastore/url-watches.json`.
`seed.py` checks both.) Confirm it works:

```bash
curl -s http://localhost:5001/api/v1/systeminfo -H "x-api-key: <token>"
# -> {"queue_size":0,"overdue_watches":[],"uptime":...,"watch_count":7,"version":"0.55.7"}
```

Key facts for changedetection **0.55.7** (discovered against the running core):

- The container is **`agentic-cores-changedetection-1`**, UI + REST on **:5001**
  (maps container :5000).
- Auth header is **`x-api-key: <token>`** (not Bearer).
- `POST /api/v1/watch` takes `{"url","title","tag"}` where **`tag` is a
  comma-separated string of tag titles** — changedetection auto-creates the tags and
  returns `{"uuid": "..."}`. Watch creation is non-destructive (it only starts
  observing a public page), so the agent does it without an approval gate.
- A watch's display state is derived from three fields: `last_checked` (epoch),
  `last_changed` (epoch; 0 = no diff yet), and `last_error` (bool). "Unread" =
  `last_changed > 0 and not viewed`.
- Newly-seeded watches have no diffs yet (`last_changed: 0`) — expected. Pages that
  block the fetcher surface as `last_error` → a **"check error"** state on the
  dashboard (a real, useful signal, not a crash).

## Seed + run

```bash
cd apps/market-radar

# 1. Seed changedetection (idempotent — safe to re-run; writes .env with the live token)
python3 seed.py
#   → Found api_access_token in /datastore/changedetection.json
#   → systeminfo OK: version=0.55.7 watch_count=2
#   → created 5 watches; SEED_OK watches_created=5 total_watches=7
#   → Wrote .env (CD_API_URL, CD_API_KEY, CD_FRONT_URL)

# 2. Install deps + run the service
pip install -r requirements.txt          # add --break-system-packages on PEP-668 hosts
python3 -m uvicorn app:app --host 0.0.0.0 --port 8204
#   app.py auto-loads .env, so CD_API_KEY is picked up with no manual copy.

# Or with Docker (point CD_API_URL at the changedetection service, not localhost):
docker build -t agentic-market-radar .
docker run --rm -p 8204:8204 \
  -e CD_API_URL=http://host.docker.internal:5001 \
  -e CD_API_KEY=<token from .env> \
  -e CD_FRONT_URL=http://localhost:5001 \
  agentic-market-radar
```

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `CD_API_URL` | `http://localhost:5001` | changedetection REST base (`/api/v1`). |
| `CD_API_KEY` | _(from .env)_ | `x-api-key` token = `settings.application.api_access_token`. |
| `CD_FRONT_URL` | `http://localhost:5001` | changedetection UI link for the "Open in changedetection ↗" button (the hybrid / human-operable path). |
| `PORT` | `8204` | uvicorn bind port. |
| `ANTHROPIC_API_KEY` | _(optional)_ | If set, the `brief` action adds an LLM-written one-paragraph summary (model `claude-opus-4-8`). The endpoint works fully without it — the brief is computed from the real watch data. |

## Endpoints

- `GET /health` → `{"status":"ok","core":"changedetection","connected": <bool from /api/v1/systeminfo>}`
- `GET /api/activity` → live KPIs (competitors tracked, changes this week, price moves,
  unread changes) + the full watch list with title/tags/last_checked/last_changed/state,
  all derived from changedetection REST. Cached 15s.
- `GET /` → the MD3 market-radar dashboard rendered from the live watches. Header shows
  "Market Radar" + "Summit Roofing Co.", a green
  "agent active · core: changedetection connected" pill, a "data: live from
  changedetection" badge, and an **"Open in changedetection ↗"** button. Layout: KPI
  tiles, a competitor "watch" card grid with last-change pills, and a change-feed table.
  An alert banner appears whenever a watch has an unread change.
- `POST /agent/run` with `{"action": ...}`:
  - `"add_watch"` `{url, title, tag?}` → `POST /api/v1/watch` (creates a new monitor).
    Non-destructive → `"requires":"none"`, no approval needed.
  - `"brief"` → reads the watch list + each watch's history
    (`GET /api/v1/watch/{uuid}/history`) and summarizes recent changes. Read-only →
    no approval. Adds an LLM brief if `ANTHROPIC_API_KEY` is set (guarded).
