# Control-plane auth, idempotency cache and multi-replica limits

## Mutating endpoints require a bearer token
`AGENTIC_OS_CONTROL_TOKEN` gates every endpoint that can change state:

| Surface | Mutating (token required) | Open (read-only) |
|---|---|---|
| `agentic_os.mission.unified_server` | `POST /api/bridge/invoke`, `POST /api/missions/{id}/approve` | `/api/connections`, `/api/needs-you`, `/api/missions/*`, `/api/bridge/capabilities`, `/` |
| `agentic_os.control_plane` | `POST /up`, `/down`, `/dispatch`, `/approvals/{id}/{decision}`, `POST/DELETE /api/permissions/grants` | `/health`, `/status`, `/modules`, `/approvals`, `/jobs/*`, ... |

Send `Authorization: Bearer <token>` (compared with `hmac.compare_digest`).
* token unset (and, on the main control plane, no legacy `AGENTIC_OS_API_KEY` / `PERMISSIONS_ADMIN_KEY`) -> **503** with a message; the endpoint never runs open;
* wrong / missing credentials -> **401**; correct -> normal handling.

The main control plane still accepts the legacy `X-API-Key` (`AGENTIC_OS_API_KEY`; grants: `PERMISSIONS_ADMIN_KEY`) as an alternative. **Behaviour change:** an unset key used to mean "open"; it now means "disabled".
`python -m agentic_os.mission.unified_server` binds `127.0.0.1` (override with `UNIFIED_HOST`, port `UNIFIED_PORT`, default 8090).
`agentic_os/mission/control_plane.py` is only the wire-protocol codec (no HTTP surface), so nothing to gate there.
`/m/<name>/api/*` (module proxy) is unchanged; the public demo keeps it read-only via `DEMO_READ_ONLY=1`.

## Projects API CORS
Same-origin only by default (the bundled UI is served by the API itself). Opt in to cross-origin callers with `PROJECTS_CORS_ORIGINS=https://a.example,https://b.example`.

## Operator idempotency cache
`Operator.invoke` dedupes on `(capability, inputs, Idempotency-Key)` (exactly-once for successful side effects).
* Failure-shaped results (`mission.verify.result_failed`) and raised exceptions are **not** cached, so a retry after the backend recovers re-executes. A side-effect-free staged `pending_approval` result is cached; the approved retry carries `_approval` and so has a different key.
* Bounded LRU: `AGENTIC_OS_IDEMPOTENCY_MAX` (default 10000).
* **Per-process.** Not shared between replicas.

## Failure classifier (N4)
One classifier (`result_failed`) serves the verifier, marker synthesis and the cache. Beyond status/`ok`/`error`/HTTP-code shapes it flags: failed nested `results[]`/`items[]` elements, `failed_count > 0`, non-empty `failures`, string status codes (`"500"`), HTTP-style `{detail: "Forbidden"}` bodies, `verified: false`, and an empty `{}` from a side-effecting capability that declares no outputs.

## Multiple replicas
`ReplayGuard` (approval-token nonces) and the idempotency cache are in-memory and per-process. `agentic_os.mission.approval.SharedStore` is the protocol seam for a shared backend (e.g. Redis); none is shipped. Set `AGENTIC_OS_REPLICAS=<n>`; with n>1 a WARNING is logged at startup. Run one replica until a shared store exists.

## Projects API `POST /api/outcomes` (learning evidence)
The outcome log feeds `run_promotion_cycle`, so it is evidence, not telemetry. Two lanes:

| Lane | Selected by | Auth | Effect |
|---|---|---|---|
| **Demo / simulated** (default) | no `Authorization` header and `authoritative` not true | none (public demo stays open) | In-memory sandbox keyed by client IP (`X-Demo-Session` is only a sub-key): 50 events/session, 100/client IP, 500 sessions, 5000 global, 15 min TTL; when sessions or the global budget are exhausted the OLDEST other sessions are evicted rather than refusing everyone. Flagged `demo: true, simulated: true, namespace: "demo"`. Never written to any outcome store, never read by promotion, correlation or export. Behind a proxy (e.g. cloudflared) every request may share one peer IP: set `OUTCOMES_TRUSTED_PROXY_HEADER=cf-connecting-ip` (or `x-forwarded-for`, last entry used) AND `OUTCOMES_TRUSTED_PROXY_IPS=<proxy peer ips>`; the header is honoured only from those peers (default OFF, spoofed headers ignored). |
| **Authoritative** | `Authorization` header or `authoritative: true` | `AGENTIC_OS_CONTROL_TOKEN` bearer: unset -> 503, wrong/missing -> 401 | Appends one `OutcomeEvent`, only with a valid receipt. |

Authoritative requirements (4xx and **no append** otherwise):
* `intervention_id` (alias `decision_ref`) resolving to an **executed, non-shadow** `InterventionRecord` in the provider's `intervention_store` (404 unknown; 409 unexecuted or mismatching `execution_ref`/`mission_id`/`source_app`/`action_kind`). The store is configured with `INTERVENTION_STORE_PATH` (a durable JSONL `FileInterventionStore`, like `OUTCOME_STORE_PATH`); **unset => every authoritative write is rejected** (fail closed). Postgres intervention stores are not wired here.
* **Tenant:** `InterventionRecord.tenant_id` (default None). The request tenant must equal the receipt's tenant (None, `""` and whitespace all normalise to "untenanted"; mismatch => 409). The request tenant is currently a body **claim**: the control token is global, so this stops a receipt being replayed into other tenants' evidence but does NOT authenticate the caller as a tenant. The tenant is read from one function (`_outcome_tenant`) so it can later come from a tenant-bound token.
* `idempotency_key` (<=128 chars): the same key + tenant with an identical payload returns `duplicate: true` and appends nothing; the same key with a different payload (canonical SHA-256, kept in the event `note`) => 409.
* explicit `attribution_confidence` in [0,1]; `observed_reward` or `reward_dimensions`. All numbers must be finite; |reward| <= 1e6, 0 <= delay <= 10 years, <= 16 reward dimensions with keys <= 64 chars; violations => 422 (same limits on the demo lane). Request bodies over 16 KiB => 413, enforced by an ASGI middleware that counts streamed bytes (works for chunked bodies without `Content-Length`) on `/api/outcomes` and the promotion-cycle route.
* `source_app` / `action_kind` / `candidate_id` come from the receipt, not the caller.

`POST /api/projects/{id}/policy/promotion-cycle`: `apply=true` or any `tenant_id` requires the control token; an anonymous dry run evaluates only **untenanted** evidence. The cycle is always scoped to one tenant (no mixing).

Defence in depth: `OutcomeEvent.simulated=True` is refused by every `OutcomeStore.append` and filtered by `policy_promotion.run_promotion_cycle`. Store `append` is atomic (lock; `flock` + `O_APPEND` for the file store, multi-process safe on local POSIX filesystems only: the flock is a no-op without `fcntl` (Windows, a warning is logged once) and unreliable on NFS; use the Postgres store or a single writer there) and idempotent on `event_id` (tenant-scoped). **Changed dedupe semantics:** a derived outcome with identical (source observation ids, selected intervention) is now dropped on append (previously both were stored). Trusted in-process callers (`apps/billing`, connectors) keep using `record_outcome_event` / store `append` directly.
