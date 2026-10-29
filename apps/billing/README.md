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

for the demo tenant **Meridian Wealth Management** (a wealth-management firm / RIA).

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
    'puts Organization.find_by(name: "Meridian Wealth Management").api_keys.first.value'
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
docker run -e AGENTIC_OS_PERMISSIONS=off --rm -p 8201:8201 \
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
  "Meridian Wealth Management", a green "agent active · core: Lago connected" pill, a
  "data: live from Lago" badge, and an "Open in Lago ↗" button. An approval banner
  appears whenever there's an overdue invoice.
- `POST /agent/run` with `{"action": ...}`:
  - `"chase_overdue"` → for each overdue invoice, `POST /api/v1/invoices/{id}/retry_payment`
    (Lago's real dunning/retry action) and report what it did per invoice.
  - `"refund"` → **never executed**; returns `{"status":"pending_approval", ...}` because
    refunds are human-approved.

## Closed loop

The overdue-invoice / dunning case runs as a real runtime-native loop. Nothing in it is sample data: signals
are read from Lago, the action is a governed mission, and the outcome is read back from Lago again.

```
 observe.py          Lago invoices (+ payment_requests) -> BillingSignals -> bi-temporal Observation (known_at = our read, OBSERVED)
 producers.py        BillingSignals -> DecisionOpportunity (value from overdue amount + payment history; confidence from
                     data completeness; 24h dunning cooldown) -> priority_engine.select_action (do-nothing/abstain first-class)
 decision.py         run_decision_cycle -> bridge_selection -> MissionRuntimeLauncher over a MissionRuntime built from THIS
                     app's operator (LocalOperatorClient). Writes an InterventionRecord for every selection
                     (opportunity_id=billing:<account>). billing.dunning is approval-gated: the mission PARKS.
 approve_mission     drive(runtime, mission, approver=<explicit decision>) -> runtime delivers the approval marker ->
                     billing.dunning retries ONLY the selected account's chosen invoices (core.chase_invoices)
 verify              each invoice is GET back from Lago: retried / payment_status_after / settled. Declared outputs
                     dunning_verified (bool), settled_count, failed_count. Nothing settled, a rejected retry or a
                     partial result => status error/partial and the kernel verifier FAILS the node (never "success").
 observe_outcomes    N days later (after_days, injected clock in tests) re-read the chased invoices, derive the outcome via
                     outcome_derivation.derive_and_persist (competing executed interventions on the account discount the
                     attribution confidence), persist OutcomeEvent (FileOutcomeStore) + OutcomeLink (intervention store).
 refit_policy        the ONLY refit path: policy_promotion.run_promotion_cycle (held-out gate, apply=True required).
                     Too little data => refused with reasons. Never invoked per request.
```

Endpoints (operator HTTP Basic; **fail closed** with 503 when no credential is configured, 401 otherwise):

- `POST /api/decide` body `{"account": "<customer external_id>"}` (optional; default = account with the largest overdue
  balance). Observes Lago, runs one decision cycle, returns `opportunities`, `selected` (action, governance decision,
  utility, alternatives), `intervention_id`, `mission_id` and `mission_state` (`waiting_human` for dunning). Executes nothing.
- `POST /api/decide/{mission_id}/approve` body `{"decision": "approve"|"reject"}` (default approve). Resolves the parked
  gate; on approve the dunning runs and the response carries the per-invoice read-back (`dunning`) and the mission state
  (`succeeded` or `failed`). Caller-supplied approval keys (`_approval`, any case/nesting) are stripped from every body,
  and invoice scope never comes from the request - only from the observed state.

Environment variables (in addition to the table above):

| Var | Default | Meaning |
|-----|---------|---------|
| `OPERATOR_BASIC_AUTH_USER` / `_PASS` or `OPERATOR_BASIC_AUTH` / `OUTREACH_BASIC_AUTH` (`user:pass`) | _(unset => 503)_ | Operator credential for `/api/decide*`. |
| `BILLING_STATE_DIR` | _(unset => in-memory)_ | Directory for durable `interventions.jsonl`, `outcomes.jsonl`, `observations.jsonl`. |
| `BILLING_DUNNING_COOLDOWN_S` | `86400` | No second dunning on an account inside this window. |
| `BILLING_REFERENCE_AMOUNT_CENTS` | `100000` | Overdue amount at which the dunning value saturates (a scale, not a prediction). |
| `BILLING_VERIFY_ATTEMPTS` / `BILLING_VERIFY_INTERVAL_S` | `3` / `2.0` | Bounded read-back wait: Lago settles retried payments asynchronously. |

`billing.dunning` accepts `account_id` / `invoice_ids` (only overdue invoices of that account, intersected with
`invoice_ids`, are chased; anything else is reported as `skipped`). With no scope it keeps the legacy console behaviour
(all overdue invoices, no read-back) - the loop never uses that form.

Documented fallbacks (listed in `BillingSignals.fallbacks`, each lowers decision confidence, enough of them => abstain):
missing `payment_due_date`, absent `payment_overdue` flag (derived), unavailable `payment_requests` (no last-dunning).
Limits: overcharge detection is not derivable from Lago invoices (always false, so refunds are not proposed by the loop);
the runtime and parked missions are in-memory (a restart loses a parked approval; intervention/outcome/observation
stores survive with `BILLING_STATE_DIR`); Lago has no `paid_at`, so a payment is dated by when we observed it.

## Operating notes (approval gating, durability)

- **Approval gate lives in `core`.** `chase_overdue`, `chase_invoices`, `create_subscription`, `cancel_subscription`
  and `stage_refund` stage a `pending_approval` preview unless handed a runtime-delivered `_approval`. The app's own
  routes (`/agent/run`) obtain an approval only when the request carries valid operator Basic auth
  (`OPERATOR_BASIC_AUTH=user:pass`, or `OUTREACH_BASIC_AUTH`); unset => nobody is the operator (fail closed).
  Console/LLM tool calls never carry the marker.
- `billing.dunning` requires `account_id` (+ optional `invoice_ids`); unscoped calls are refused `scope_required`
  unless `all_accounts: true` and an approval are present. Every executed path reads the invoices back from Lago.
- **Durable closed loop.** Set `BILLING_STATE_DIR` to persist interventions, outcomes, observations, promoted
  policies and the mission event log (`missions.jsonl`); parked missions are rehydrated at startup so
  `POST /api/decide/{mission_id}/approve` works after a restart.
- `POST /api/outcomes/observe` and `POST /api/policy/refit` (operator auth). `BILLING_OUTCOME_INTERVAL_S=<seconds>`
  enables a background outcome observer (default off). Refit always goes through the promotion gate.
- Narration uses `agentic_os.app.llm.governed_text` (CUSTOMER_CONFIDENTIAL, fail closed to deterministic text).

## Authorization note

Note: basic permissions authorise the app INSTANCE, not callers: an instance with write grants lets every caller who can reach it write. See `docs/PERMISSIONS_MIGRATION.md` section 29.
