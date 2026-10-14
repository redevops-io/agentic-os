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

## Mission API (`/missions`, `/inbox`, `/cockpit`, `/capabilities`, `/learning`) - default-deny

`agentic_os.mission.api` (mounted by `mission.service.mount()` / `build_mission_app()` and by `deploy/sidekick-devops`) is guarded by ONE router-level dependency, `agentic_os.mission.api_gate.mission_gate`, attached to all three routers. A route added later is protected automatically. `api_gate.py` depends only on `control_auth` and FastAPI.

**The rules**
1. Every non-GET/HEAD/OPTIONS route requires `Authorization: Bearer <token>`; `@public` is never honoured for mutating methods.
2. **Reads are gated by default too.** Only `@public` routes are open: `/capabilities` (the global capability catalogue, not mission data), `/buildinfo`, and the static `/cockpit` HTML shell.
3. A token that is presented but invalid is **401 on every route** (public ones included), never silently treated as anonymous.
4. `/learning` (global learner state) is `@admin_only`: tenant/approver tokens get 403.
5. `tests/test_mission_api_default_deny.py` introspects the app and fails if any POST/PUT/PATCH/DELETE route lacks the dependency.

| Env var | Meaning |
|---|---|
| `AGENTIC_OS_CONTROL_TOKEN` | Admin token (same as the other control surfaces). Acts on all tenants. |
| `AGENTIC_OS_TENANT_TOKENS` | Optional `tenantA=<tokA>,tenantB=<tokB>`. A tenant (requester) token can only create/read/approve/simulate missions whose `tenant` equals its own; the tenant comes from the token, never a header or body. Cross-tenant and untenanted missions return **404**, identical to "not found". Reads (list, inbox, events...) are filtered to the tenant. |
| `AGENTIC_OS_APPROVER_TOKENS` | Optional `tenantA=<tokA2>`: separation of duty. When set (for any tenant), requester tokens get **403** on `/approve`; only the tenant's approver token (or the admin token) can approve. Approver tokens can read their tenant and approve but cannot create or simulate. **When unset, requester == approver** (the token that created a mission can approve its gates). |
| `AGENTIC_OS_GATE_READS` | Default **on**. `0`/`false`/`no`/`off` opts out so reads are open and unscoped for anonymous callers. **Unsafe off loopback**: anyone can read every tenant's goals, inputs, events and evidence. Mutations stay gated either way. |

Invalid config fails closed: a token shared by two tenants, a tenant/approver token equal to another token, or a duplicate tenant raises `ConfigError` at `mount()` and answers **503** with the reason from the gate.
* neither token variable set -> **503**; missing/wrong token -> **401**; trailing-slash 307 redirects land on the gated route.
* Approvals: bearer token + tenant ownership (+ approver credential if configured); `decision` is validated; `_approval*` keys are stripped from `edit`; body flags such as `approved: true` are ignored. The signed approval token that operators verify at `/invoke` (`mission/approval.py`) is unchanged.
* Cockpit page: escapes text and attribute contexts, uses `data-*` + one delegated listener (no inline handlers), and asks for the token in an in-page form kept **in memory only** (lost on reload; nothing in web storage).

### Deployment notes
* **Any deployment that mounts the mission routers must set `AGENTIC_OS_CONTROL_TOKEN`** (and/or `AGENTIC_OS_TENANT_TOKENS`), otherwise mutations and reads answer 503. With reads gated by default the cockpit shell loads but needs a token (or the explicit `AGENTIC_OS_GATE_READS=0` opt-out on a private network) to show data.
* The stack repo's `integrated.compose.yml` `control-plane` service (`agentic_os.control_plane:app`, port 8091, profile `v3`, legacy) mounts these routers via `service.mount` without a token: set `AGENTIC_OS_CONTROL_TOKEN` there before enabling the profile.
* `deploy/k3s/mission-demo/am_service.py` (stack repo) imports the same routers with CORS `*` and defines its own `/book/run` and `/missions/{mid}/stream` routes outside this gate: put them behind `mission_gate` (or the same bearer check) and drop wildcard CORS - tracked as a follow-up, not covered by this repo's PR.
* `deploy/sidekick-devops/sidekick_server.py` inherits the gate; set the token env on that service.
* The enterprise fork (`agentic_os_enterprise/mission/api.py`) is patched separately.

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
