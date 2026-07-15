> ### Reference application for [Context Runtime](https://github.com/redevops-io/context-runtime)
>
> A focused AI system for **billing & collections**. Context Runtime ships a tenant that learns **which usage/invoice/dunning signals to pull before a credit or collections action** — in its offline benchmark the learned policy scores **4.122 vs 2.442** against a full-stack baseline ([`examples/agentic_billing.py`](https://github.com/redevops-io/context-runtime/blob/main/examples/agentic_billing.py)).
>
> ```
> Context Runtime  →  ReDevOps RAG  →  Sidekick  →  Application logic
> ```
> One of the [ReDevOps](https://github.com/redevops-io) reference applications built on Context Runtime.

---

# agentic-billing — agent layer + dashboard over a real Lago core

The **first** "agentic module on a real OSS core" vertical slice, and the reference
pattern every other module copies. It wraps the running self-hosted **Lago** instance
(the open-source billing core) with:

- an **agent layer** that reads REAL Lago data over its REST API, and
- an **MD3 dashboard** rendered from that live data (no mock data),

for the demo tenant **Summit Roofing Co.** (a roofing contractor).

```
Lago (OSS core, :3000) ──REST──▶ app.py (FastAPI, :8201) ──▶ MD3 dashboard + /api/activity + /agent/run
        ▲                                                         agentic actions (chase_overdue, refund)
        └── seed.py / seed.rb bootstrap org + customers + invoices (idempotent)
```

## Files

| File | Purpose |
|------|---------|
| `seed.rb` | Idempotent Ruby seed run via `rails runner` inside the Lago API container. Creates the org, billing entity, API key, 5 customers, 2 plans, 1 metric, and 11 invoices. |
| `seed.py` | Repeatable wrapper: copies `seed.rb` into the container, runs it, captures the API key, writes `.env`. |
| `app.py` | FastAPI service (port 8201): `/health`, `/api/activity`, `/` dashboard, `/agent/run`. |
| `requirements.txt` | fastapi, uvicorn, httpx. |
| `Dockerfile` | slim-python image running `uvicorn app:app --port 8201`. |
| `.env` | Written by `seed.py`: `LAGO_API_URL`, `LAGO_API_KEY`, `LAGO_FRONT_URL`. |

## Lago bootstrap method (the one that worked)

Self-hosted Lago needs an Organization + API key before its REST API works. The most
reliable bootstrap is **`rails runner` inside the Lago API container** (the `api`
container is the Rails app):

```bash
sudo docker exec lago-api bundle exec rails runner /tmp/seed.rb
```

Key facts for Lago **v1.48** (discovered by inspecting the running models):

- The API container is **`lago-api`** (not `api`).
- Save records **with validations on** (plain `save!`) — `save!(validate: false)` skips
  the `before_validation` callbacks that populate the NOT-NULL `slug` columns and
  sequence ids, and trips a `NotNullViolation`.
- A **billing entity** is required to attach invoices; the first one created becomes the
  org's `default_billing_entity`.
- The **API key lives in its own `ApiKey` model** (`org.api_keys`), not on the
  Organization row. Its `value` is **auto-generated** (a UUID) on create and cannot be
  set to a chosen string — so the seed reads the generated value back and reuses the same
  key on re-runs. **Where to find it:** `seed.py` prints `API_KEY=<value>` and writes it
  to `.env`; or query directly:
  ```bash
  sudo docker exec lago-api bundle exec rails runner \
    'puts Organization.find_by(name: "Summit Roofing Co.").api_keys.first.value'
  ```
- Invoice state is three fields: `status` (draft/finalized/voided…), `payment_status`
  (pending/succeeded/failed), and the `payment_overdue` boolean.

## Seed + run

```bash
cd apps/billing

# 1. Seed Lago (idempotent — safe to re-run; writes .env with the live API key)
python3 seed.py
#   → SEED_OK org=... customers=5 invoices=11 collected_mtd=148700.0 overdue=2
#   → API_KEY=<uuid>   (also written to .env)

# 2. Install deps + run the service
pip install -r requirements.txt          # add --break-system-packages on PEP-668 hosts
python3 -m uvicorn app:app --host 0.0.0.0 --port 8201
#   app.py auto-loads .env, so LAGO_API_KEY is picked up with no manual copy.

# Or with Docker (point LAGO_API_URL at the Lago api service, not localhost):
docker build -t agentic-billing .
docker run --rm -p 8201:8201 \
  -e LAGO_API_URL=http://host.docker.internal:3000 \
  -e LAGO_API_KEY=<key from .env> \
  -e LAGO_FRONT_URL=http://localhost:80 \
  agentic-billing
```

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `LAGO_API_URL` | `http://localhost:3000` | Lago REST base (`/api/v1`, `/health`). |
| `LAGO_API_KEY` | _(from .env)_ | Bearer token — the `ApiKey.value` from the seed. |
| `LAGO_FRONT_URL` | `http://localhost:80` | Lago UI link for the "Open in Lago ↗" button (the hybrid / human-operable path). |
| `PORT` | `8201` | uvicorn bind port. |
| `ANTHROPIC_API_KEY` | _(optional)_ | If set, `/agent/run` adds an LLM reasoning blurb (model `claude-opus-4-8`). The endpoint works fully without it — actions are deterministic Lago API calls. |

## Endpoints

- `GET /health` → `{"status":"ok","core":"lago","connected": <bool from Lago /health>}`
- `GET /api/activity` → live KPIs (collected MTD, outstanding, payment-success %, active
  customers) + recent invoices + overdue list, all derived from Lago REST. Cached 15s.
- `GET /` → the MD3 billing dashboard rendered from the live data. Header shows
  "Summit Roofing Co.", a green "agent active · core: Lago connected" pill, a
  "data: live from Lago" badge, and an "Open in Lago ↗" button. An approval banner
  appears whenever there's an overdue invoice.
- `POST /agent/run` with `{"action": ...}`:
  - `"chase_overdue"` → for each overdue invoice, `POST /api/v1/invoices/{id}/retry_payment`
    (Lago's real dunning/retry action) and report what it did per invoice.
  - `"refund"` → **never executed**; returns `{"status":"pending_approval", ...}` because
    refunds are human-approved.
