# agentic-privacy — DSAR automation (GDPR access/erasure · CCPA/CPRA delete/opt-out)

An agent that fulfils data-subject / consumer requests by **fanning out across every
personal-data store**, assembling an access export, executing a cascading erasure, or
recording an opt-out — verified, audited, and on the SLA clock (GDPR 30d / CCPA 45d).
The on-brand answer to "how do we actually fulfil privacy requests across all our
systems" — and a sellable module of the Agentic Business OS.

## Endpoints
| route | what |
|---|---|
| `GET /` | MD3 dashboard: open/closed requests, SLA timers, per-system connector coverage |
| `GET /api/activity` · `GET /health` | same data as JSON / liveness + live connectors |
| `POST /request` | file a request (below) |

## `POST /request`
```json
{"type":"access|delete|opt_out|correct","email":"subject@x.com","verified":true,"confirm":true}
```
- **access** → fans out `find(email)` across all connectors, returns the records + an
  LLM-drafted plain-language export.
- **delete** → **dry-run preview by default**; executes the cascading erasure only when
  `verified:true` AND `confirm:true` (identity verification gate).
- **opt_out** → records the CCPA opt-out (marketing/sale-share is also governed by the
  site consent banner + Global Privacy Control).
- Every call writes to a tamper-evident **audit log** (`/data/audit.jsonl`) + the
  requests store (`/data/requests.json`).

## Connectors
Real (wired): **erpnext** (Lead/Contact/Communication), **listmonk** (subscribers),
**chatwoot** (contacts). Stubs (honest "not wired" on the dashboard): **zitadel,
billing, s3, posthog** — each is one `find`/`delete` function away.

## Config (env)
`ERPNEXT_URL/API_KEY/API_SECRET`, `LISTMONK_API_URL/USER/TOKEN`,
`CHATWOOT_API_URL/API_TOKEN/ACCOUNT_ID`, `REDEVOPS_LLM_BASE_URL/MODEL`,
`PRIVACY_DATA_DIR` (volume), `PRIVACY_SLA_DAYS` (30 GDPR / 45 CCPA).

> Identity verification is the `verified` flag — production must add a real emailed-
> confirmation step before any erasure. This module never deletes without
> `verified=true AND confirm=true`.

## Identity, confirmation and env (hardened DSAR flow)

- Identity is proven only by redeeming the emailed single-use token at `/verify?id=&token=`. `/request` and `/intake` only open PENDING requests.
- Delete/correct execute only for a `verified` request id bound to the email, with operator confirmation: the runtime approval gate (`privacy.delete {request_id, email}`) or `POST /fulfil/{request_id}` with Basic auth. After acting, connectors are re-queried; status is `erased`/`corrected` only if the read-back is clean, else `partial_erasure`/`partial_correction`.
- `POST /retention` is a counts-only dry run unless operator Basic auth AND `confirm:true`.
- Env: `OPERATOR_BASIC_AUTH_USER`/`OPERATOR_BASIC_AUTH_PASS` (or `OPERATOR_BASIC_AUTH`/`OUTREACH_BASIC_AUTH` = `user:pass`; unset => fail closed), `PRIVACY_DEV_MODE` (expose verify link; default false), `PRIVACY_DEMO_MODE` (one-click flow on synthetic data; refuses to start with real connectors configured), `PRIVACY_VERIFY_TTL_SECONDS` (86400), `PRIVACY_EXPORT_TTL_SECONDS` (600), `PRIVACY_LLM_SUMMARY` (counts/categories-only narration; default off).
