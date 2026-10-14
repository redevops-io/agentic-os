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
| **Demo / simulated** (default) | no `Authorization` header and `authoritative` not true | none (public demo stays open) | Stored in an in-memory, per-session (`X-Demo-Session`, else client IP) sandbox: 50 events/session, 500 sessions, 1h TTL. Response is flagged `demo: true, simulated: true, namespace: "demo"`. Never written to `OutcomeStore`/`FileOutcomeStore`, never read by promotion, correlation or export. |
| **Authoritative** | `Authorization` header or `authoritative: true` | `AGENTIC_OS_CONTROL_TOKEN` bearer: unset -> 503, wrong/missing -> 401 | Appends one `OutcomeEvent`, only if the request carries a receipt. |

Authoritative requirements (4xx and **no append** otherwise): `intervention_id` (alias `decision_ref`) resolving to an **executed, non-shadow** `InterventionRecord` in the provider's `intervention_store` (404 unknown, 409 unexecuted / `execution_ref`, `mission_id`, `source_app`, `action_kind` mismatch); `idempotency_key` (same key + tenant => same result, no second append, `duplicate: true`); explicit `attribution_confidence` in [0, 1] (no default); `observed_reward` or `reward_dimensions`. `source_app` / `action_kind` / `candidate_id` are taken from the receipt, not the caller. Optional `tenant_id` is stored on the event; `run_promotion_cycle(tenant_id=...)` evaluates only that tenant's events.

Defence in depth: `OutcomeEvent.simulated=True` is refused by every `OutcomeStore.append` and filtered by `policy_promotion.run_promotion_cycle`. `OutcomeStore.append` is idempotent on `event_id` (tenant-scoped) and, for derived outcomes, on (source observation ids, selected intervention). In-process app/connector code keeps using the trusted `record_outcome_event` / store `append` directly (e.g. `apps/billing`).
