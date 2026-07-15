> ### Reference application for [Context Runtime](https://github.com/redevops-io/context-runtime)
>
> A focused AI system for **bookkeeping & close**. Context Runtime ships a tenant that learns **which ledgers/reports to pull per books question** — in its offline benchmark the learned policy scores **3.632 vs 2.430** against a full-books baseline ([`examples/agentic_books.py`](https://github.com/redevops-io/context-runtime/blob/main/examples/agentic_books.py)).
>
> ```
> Context Runtime  →  ReDevOps RAG  →  Sidekick  →  Application logic
> ```
> One of the [ReDevOps](https://github.com/redevops-io) reference applications built on Context Runtime.

---

# agentic-books — agent layer + dashboard over a real ERPNext core

The **9th** agentic module, built to the same reference pattern as
[`agentic-billing`](../billing) (Lago) but wrapping the running self-hosted **ERPNext**
core (the open-source accounting/ERP platform). It gives the demo tenant **Summit Roofing
Co.** a QuickBooks/Xero-style books experience:

- an **agent layer** that reads REAL ERPNext data over its REST API, and
- an **MD3 dashboard** rendered from that live data (no mock data),

```
ERPNext (OSS core, :8092) ──REST──▶ app.py (FastAPI, :8209) ──▶ MD3 books dashboard
        ▲                                                        /api/activity + /agent/run
        │                                                        actions: categorize, reconcile, close*
        └── seed.py / seed_erpnext.py bootstrap company + invoices + bank txns (idempotent)
                                                                 (* close = approval-gated)
```

## Files

| File | Purpose |
|------|---------|
| `seed_erpnext.py` | Idempotent frappe seed run **inside** the ERPNext backend container via the bench virtualenv python. Creates Fiscal Years, the Company "Summit Roofing Co." (builds the standard Chart of Accounts), 5 customers, 4 suppliers, an item, ~7 Sales Invoices (some paid → A/R), 4 Purchase Invoices (→ A/P), 2 Journal Entries (payroll + depreciation), Payment Entries, and 7 unreconciled Bank Transactions. |
| `seed.py` | Host wrapper: copies `seed_erpnext.py` into the container, runs it over an optional `ssh` host + `docker`, (re)generates the Administrator API key/secret, writes `.env`. |
| `app.py` | FastAPI service (port 8209): `/health`, `/api/activity`, `/` dashboard, `/agent/run`. |
| `requirements.txt` | fastapi, uvicorn, httpx. |
| `Dockerfile` | slim-python image running `uvicorn app:app --port 8209`. |
| `.env` | Written by `seed.py`: `ERPNEXT_URL`, `ERPNEXT_API_KEY`, `ERPNEXT_API_SECRET`, `ERPNEXT_FRONT_URL`, `COMPANY`. **gitignored.** |

## ERPNext bootstrap method (the one that worked)

ERPNext (Frappe v15/16) needs a Company + Chart of Accounts + an API key before its REST
API is useful. The reliable bootstrap on this `frappe_docker` install:

```bash
# Run the idempotent frappe seed with the bench virtualenv python (boots a frappe context):
sudo docker exec erpnext-backend-1 bash -lc \
  'cd /home/frappe/frappe-bench/sites && FRAPPE_SITE=frontend \
   /home/frappe/frappe-bench/env/bin/python /tmp/seed_erpnext.py'
```

Key facts for this **v15/16** install (discovered by inspecting the running site):

- The backend container is **`erpnext-backend-1`**; the site is **`frontend`**.
- **Don't pipe a multi-line script into `bench console`** — it's IPython, and over
  `docker exec` its continuation prompts swallow lines and buffer stdout. Run
  `env/bin/python seed_erpnext.py` instead; the script calls `frappe.init()/connect()`
  itself when there's no request context.
- **API auth:** ERPNext uses an **API key + secret** with header
  `Authorization: token <api_key>:<api_secret>`. On a freshly restored/created site the
  stored `api_secret` may fail to decrypt (`Encryption key is invalid`) — **regenerate the
  keys** (`User.Administrator.api_key/api_secret`, then `save`), which re-encrypts the
  secret with the site's current `encryption_key`. `seed.py` does this and verifies the
  round-trip decrypt before writing `.env`.
- **Bare-site fixtures:** a brand-new site is missing standard fixtures the Company's
  post-save hooks need (`Warehouse Type: Transit`, root `Item Group`/`Customer
  Group`/`Supplier Group`/`Territory`, UOM `Nos`, `Standard Selling`/`Standard Buying`
  price lists). The seed creates these first.
- **Mandatory fields:** Company needs `valuation_method`; Customer/Supplier need a
  `*_type`; Sales/Purchase Invoice need a price list + `plc_conversion_rate`; Journal
  Entry needs `voucher_type` and a `cost_center` on P&L account rows.
- The stock **"Notification for new fiscal year"** template raises on a bare site; the
  seed sets `frappe.flags.in_import` during seeding so notifications are skipped (process-
  scoped, not a persistent config change).
- **Pick income/expense accounts by `root_type`**, not name fragment — `"Sales"` would
  otherwise match `Commission on Sales`, which is an **Expense** account in the standard
  CoA, and revenue would never show as Income.

## Seed + run

```bash
cd apps/books

# 1. Seed ERPNext (idempotent — safe to re-run; writes .env with live API keys)
python3 seed.py
#   → SEED_OK company=Summit Roofing Co. revenue_mtd=148200 ar=55500 ap=58600 uncategorized=7
#   → API_KEY=<key>   (key + secret written to .env)

# 2. Install deps + run the service
pip install -r requirements.txt          # add --break-system-packages on PEP-668 hosts
python3 -m uvicorn app:app --host 0.0.0.0 --port 8209
#   app.py auto-loads .env, so the API key/secret are picked up with no manual copy.

# Or with Docker (point ERPNEXT_URL at the host gateway, not localhost):
docker build -t agentic-books .
docker run --rm -p 8209:8209 \
  -e ERPNEXT_URL=http://host.docker.internal:8092 \
  -e ERPNEXT_API_KEY=<key> -e ERPNEXT_API_SECRET=<secret> \
  -e ERPNEXT_FRONT_URL=http://localhost:8092 \
  -e COMPANY="Summit Roofing Co." \
  agentic-books
```

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `ERPNEXT_URL` | `http://localhost:8092` | ERPNext REST base (`/api/resource`, `/api/method`). In a container use `http://host.docker.internal:8092`. |
| `ERPNEXT_API_KEY` | _(from .env)_ | Administrator API key. |
| `ERPNEXT_API_SECRET` | _(from .env)_ | Administrator API secret (`Authorization: token key:secret`). |
| `ERPNEXT_FRONT_URL` | `http://localhost:8092` | ERPNext UI link for the "Open in ERPNext ↗" button. |
| `COMPANY` | `Summit Roofing Co.` | The books company. |
| `PORT` | `8209` | uvicorn bind port. |
| `REDEVOPS_LLM_BASE_URL` / `REDEVOPS_LLM_MODEL` | _(optional)_ | Local OpenAI-compatible LLM (DeepSeek) for `/agent/run` narration. Fully guarded — actions are deterministic ERPNext API calls and work without it. |
| `ANTHROPIC_API_KEY` | _(optional)_ | Fallback narration via Claude (`claude-opus-4-8`). |

## Endpoints

- `GET /health` → `{"status":"ok","core":"erpnext","connected": <bool>}` (connected comes
  from a cheap `frappe.auth.get_logged_user` call).
- `GET /api/activity` → live KPIs (cash position, net income/P&L, A/R from outstanding
  Sales Invoices, A/P from outstanding Purchase Invoices) + the uncategorized Bank
  Transaction queue + the month-end close checklist/progress, all derived from ERPNext.
  Cached 15s.
- `GET /` → the MD3 books dashboard rendered from the live data. Header shows "Summit
  Roofing Co.", a green "agent active · core: ERPNext connected" pill, a "data: live from
  ERPNext" badge, and an "Open in ERPNext ↗" button. An approval banner appears while the
  month-end close is pending.
- `POST /agent/run` with `{"action": ...}`:
  - `"categorize"` → categorize one uncategorized Bank Transaction (writes a category hint
    onto it via the REST API).
  - `"reconcile"` → match a deposit to an open A/R invoice and report the proposed match.
  - `"close"` → **approval-gated** (`approval_required: ["close"]`); returns
    `{"status":"pending_approval", ...}` and never posts the Period Closing Voucher without
    a human sign-off (mirrors the billing `refund` gate).
