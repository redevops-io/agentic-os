> ### Reference application for [Context Runtime](https://github.com/redevops-io/context-runtime)
>
> A focused AI system for **customer support**. Context Runtime ships a tenant that learns **which KB docs, prior tickets and account context to retrieve per ticket** — in its offline benchmark the learned policy scores **3.679 vs 2.394** against a full-context baseline ([`examples/agentic_support.py`](https://github.com/redevops-io/context-runtime/blob/main/examples/agentic_support.py)).
>
> ```
> Context Runtime  →  ReDevOps RAG  →  Sidekick  →  Application logic
> ```
> One of the [ReDevOps](https://github.com/redevops-io) reference applications built on Context Runtime.

---

# agentic-support — agent layer + dashboard over a real Chatwoot core

The support vertical slice, built on the **agentic-billing** reference pattern. It wraps
the running self-hosted **Chatwoot** instance (the open-source shared-inbox / support
core) with:

- an **agent layer** that reads REAL Chatwoot data over its REST API, and
- an **MD3 dashboard** (Zendesk/Intercom-style: KPI tiles, channel-breakdown bars, a
  live ticket queue with priority pills + age) rendered from that live data — no mock data,

for the demo tenant **Meridian Wealth Management** (a registered investment advisory firm).

```
Chatwoot (OSS core, :3003) ──REST──▶ app.py (FastAPI, :8207) ──▶ MD3 dashboard + /api/activity + /agent/run
        ▲                                                          agentic actions (draft_reply, resolve, escalate)
        └── seed.py / seed.rb bootstrap super-admin + account + inbox + contacts + 7 conversations (idempotent)
```

## Files

| File | Purpose |
|------|---------|
| `../../cores/chatwoot.compose.yml` | The Chatwoot core: `rails` (web, host 3003→3000), `sidekiq` (1 worker), `postgres` (`pgvector/pgvector:pg16`), `redis`. `seccomp=unconfined, apparmor=unconfined` on each. |
| `seed.rb` | Idempotent Ruby seed run via `rails runner` inside the Chatwoot rails container. Creates the super-admin user, account, API inbox, 2 contacts, 7 client-service conversations across open/pending/resolved, and reads the agent access token back. |
| `seed.py` | Repeatable wrapper: copies `seed.rb` into the container, runs it, captures `ACCESS_TOKEN` + `ACCOUNT_ID`, writes `.env`. |
| `app.py` | FastAPI service (port 8207): `/health`, `/api/activity`, `/` dashboard, `/agent/run`. |
| `requirements.txt` | fastapi, uvicorn, httpx. |
| `Dockerfile` | slim-python image running `uvicorn app:app --port 8207`. |
| `.env` | Written by `seed.py`: `CHATWOOT_API_URL`, `CHATWOOT_API_TOKEN`, `CHATWOOT_ACCOUNT_ID`, `CHATWOOT_FRONT_URL`. |

## Chatwoot bootstrap method (the one that worked)

Self-hosted Chatwoot needs its schema + a super-admin before its API is usable. The
reliable bootstrap is the project's own one-time rake task **inside the rails container**:

```bash
cd cores
sudo docker compose -f chatwoot.compose.yml up -d postgres redis
# one-time: creates the full schema (92 tables) + super admin
sudo docker compose -f chatwoot.compose.yml run --rm rails bundle exec rails db:chatwoot_prepare
sudo docker compose -f chatwoot.compose.yml up -d            # rails + sidekiq
```

Key facts discovered while bringing it up:

- The web service answers `/api` with **200** as soon as Rails is ready; `GET /` returns
  a **302** redirect to `/installation/onboarding` (or `/app/login`) — a 302 here means
  Rails is *healthy*, not down.
- The **API access token** is a `User#access_token.token` (Chatwoot auto-creates an
  `AccessToken` per user via a model callback). It is sent on every API call as the
  **`api_access_token`** header (not a Bearer token). **Where to find it:** `seed.py`
  prints `ACCESS_TOKEN=<value>` and writes it to `.env`; or read it directly:
  ```bash
  sudo docker exec agentic-chatwoot-rails-1 bundle exec rails runner \
    'puts User.find_by(email: "admin@meridianwealth.com").access_token.token'
  ```
  In the UI it lives under **Profile Settings → Access Token**.
- Conversations are listed per account and **filtered by status** with
  `GET /api/v1/accounts/{id}/conversations?status=open|pending|resolved` (the default
  list returns only `open`).
- A conversation's opening customer message is the message with `message_type == 0`
  (incoming); `message_type == 1` is outgoing, `2` is an activity event.

## Seeded data — "Meridian Wealth Management" (account 1, inbox 1)

7 client-service conversations across 2 contacts (Whitfield Family Trust, Okonkwo Holdings):

| # | Priority | Status | Channel | Ticket |
|---|----------|--------|---------|--------|
| 6 | urgent | open | Phone | Account access — unrecognized login alert, locked out |
| 3 | high | pending | Email | Beneficiary update — change on the trust accounts |
| 1 | medium | open | Website | Statement access — can't log in to download quarterly statement |
| 5 | medium | open | Website | Performance report — YTD returns request |
| 4 | medium | pending | Email | Advisory-fee question — fee higher than last quarter |
| 2 | low | open | Phone | ACAT transfer — status of incoming account transfer |
| 7 | low | resolved | Facebook | Tax documents — when will 1099s be available? |

→ `SEED_OK account=1 inbox=1 contacts=2 conversations=7 open=4 pending=2 resolved=1`
(re-run reports `new=0` — fully idempotent).

## Seed + run

```bash
cd apps/support

# 1. Seed Chatwoot (idempotent — writes .env with the live access token + account id)
python3 seed.py

# 2. Install deps + run the service
pip install -r requirements.txt          # add --break-system-packages on PEP-668 hosts
python3 -m uvicorn app:app --host 0.0.0.0 --port 8207
#   app.py auto-loads .env, so CHATWOOT_API_TOKEN is picked up with no manual copy.

# Or with Docker (point CHATWOOT_API_URL at the Chatwoot rails service, not localhost):
docker build -t agentic-support .
docker run --rm -p 8207:8207 \
  -e CHATWOOT_API_URL=http://host.docker.internal:3003 \
  -e CHATWOOT_API_TOKEN=<token from .env> \
  -e CHATWOOT_ACCOUNT_ID=1 \
  -e CHATWOOT_FRONT_URL=http://localhost:3003 \
  agentic-support
```

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `CHATWOOT_API_URL` | `http://localhost:3003` | Chatwoot REST base. |
| `CHATWOOT_API_TOKEN` | _(from .env)_ | Agent access token — sent as the `api_access_token` header. |
| `CHATWOOT_ACCOUNT_ID` | `1` | The account id (Meridian Wealth Management). |
| `CHATWOOT_FRONT_URL` | `http://localhost:3003` | Chatwoot UI link for the "Open in Chatwoot ↗" button (the hybrid / human-operable path). |
| `PORT` | `8207` | uvicorn bind port. |
| `ANTHROPIC_API_KEY` | _(optional)_ | If set, `draft_reply` writes the draft with Claude (`claude-opus-4-8`); otherwise a deterministic template is used. Either way the draft is posted as a private note. |

## Endpoints

- `GET /health` → `{"status":"ok","core":"chatwoot","connected": <bool>}` (connected is
  true only when the token actually authenticates against the conversations API).
- `GET /api/activity` → live KPIs (open tickets, first-response %, resolved, CSAT) +
  channel breakdown + the ticket queue, all derived from Chatwoot REST. Cached 15s.
- `GET /` → the MD3 support dashboard rendered from the live conversations. Header shows
  "Meridian Wealth Management", a green "agent active · core: Chatwoot connected" pill, a
  "data: live from Chatwoot" badge, and an **"Open in Chatwoot ↗"** button. An escalation
  banner appears whenever there's an urgent/high open ticket.
- `POST /agent/run` with `{"action": ..., "conversation_id": N}`:
  - `"draft_reply"` → drafts a reply (Claude if `ANTHROPIC_API_KEY` is set, else a
    deterministic template) and posts it as a **PRIVATE NOTE**
    (`POST /conversations/{id}/messages` with `message_type:"outgoing", private:true`).
    The customer never sees it — **sending a public reply is the human-reviewable step.**
  - `"resolve"` → `POST /conversations/{id}/toggle_status` (open ↔ resolved).
  - `"escalate"` → `POST /conversations/{id}/toggle_priority` (urgent) +
    `POST /conversations/{id}/assignments` (assign to the agent).
