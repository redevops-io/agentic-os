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

## Mission API (`/missions`, `/inbox`, `/cockpit`, `/capabilities`) - default-deny

`agentic_os.mission.api` (mounted by `mission.service.mount()` / `build_mission_app()` and by `deploy/sidekick-devops`) is guarded by ONE router-level dependency, `agentic_os.mission.api_gate.mission_gate`, attached to all three routers. A route added later is protected automatically.

**The rule:** every route whose method is not GET/HEAD/OPTIONS requires `Authorization: Bearer <token>`; `@public` is never honoured for mutating methods. `tests/test_mission_api_default_deny.py` introspects the app and fails if any POST/PUT/PATCH/DELETE route lacks the dependency.

| Env var | Meaning |
|---|---|
| `AGENTIC_OS_CONTROL_TOKEN` | Admin token (same one as the other control surfaces). Acts on all tenants. |
| `AGENTIC_OS_TENANT_TOKENS` | Optional `tenantA=<tokenA>,tenantB=<tokenB>`. A tenant token can only create/see/approve/simulate missions whose `tenant` equals its own; the tenant is taken from the token, never from a header or body. Cross-tenant (and untenanted) missions return **404**, identical to "not found". A tenant token equal to the admin token is ignored. |
| `AGENTIC_OS_GATE_READS` | `1` = also require a token for every non-`@public` GET (`/missions`, `/missions/{id}`, `/plans`, `/events`, `/explain`, `/inbox`, `/learning`). Default off for compatibility. |

* neither token variable set -> **503** (mutations never run open); missing/wrong token -> **401**.
* **Sensitive reads:** the event timeline, plans, explain, mission list and inbox expose goals, node inputs/results and evidence. They are open by default (existing behaviour) - **set `AGENTIC_OS_GATE_READS=1` on any non-loopback deployment.** `/capabilities`, `/buildinfo`, `/cockpit` (static HTML) are `@public`. When a valid tenant token is presented on an open read, results are still scoped to that tenant.
* **Approvals:** `POST /missions/{id}/approve` needs the bearer token AND tenant ownership; `decision` is validated; `_approval*` keys are stripped from `edit`, and body flags such as `approved: true` are ignored. The signed approval token that operators verify at `/invoke` (`mission/approval.py`) is unchanged. A separate approver credential (distinct from the control token) is not yet implemented.
* Use `@public` (from `api_gate`) only for deliberately public, read-only routes.
* The cockpit page prompts for the token on a 401/503 and keeps it in `sessionStorage`.

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
