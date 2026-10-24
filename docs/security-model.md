# Security model

Control plane, security profile and the operator authority boundary, approval tokens, permissions and gate notifications. Secrets are in [SECRETS.md](SECRETS.md).

## Contents

- [Control-plane auth, idempotency cache and multi-replica limits](#control-plane-auth-idempotency-cache-and-multi-replica-limits) (was `CONTROL_PLANE_SECURITY.md`)
- [Security profile and the operator authority boundary](#security-profile-and-the-operator-authority-boundary) (was `SECURITY_PROFILE.md`)
- [Signed approval tokens (HTTP operators)](#signed-approval-tokens-http-operators) (was `APPROVAL_TOKENS.md`)
- [Permissions plane — access-control admin](#permissions-plane--access-control-admin) (was `permissions.md`)
- [Gate notifications](#gate-notifications) (was `GATE_NOTIFICATIONS.md`)

## Control-plane auth, idempotency cache and multi-replica limits

### Mutating endpoints require a bearer token
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

### Mission API (`/missions`, `/inbox`, `/cockpit`, `/capabilities`, `/learning`) - default-deny

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
| `AGENTIC_OS_TENANT_LIMITS` | Optional JSON `{"tenantA": {"policy_refs": ["x:read"], "budget_usd": 25}}`. A tenant caller's grants and spend ceiling come from here, never from the request: a wildcard grant is refused, requested grants are intersected with the list, and the budget is clamped. A tenant with no entry gets no grants. |
| `AGENTIC_OS_APPROVER_TOKENS` | Optional `tenantA=<tokA2>`: separation of duty. When set (for any tenant), requester tokens get **403** on `/approve`; only the tenant's approver token (or the admin token) can approve. Approver tokens can read their tenant and approve but cannot create or simulate. **When unset, requester == approver** (the token that created a mission can approve its gates). |
| `AGENTIC_OS_GATE_READS` | Default **on**. `0`/`false`/`no`/`off` opts out so reads are open and unscoped for anonymous callers. **Unsafe off loopback**: anyone can read every tenant's goals, inputs, events and evidence. Mutations stay gated either way. |

Invalid config fails closed: a token shared by two tenants, a tenant/approver token equal to another token, or a duplicate tenant raises `ConfigError` at `mount()` and answers **503** with the reason from the gate.
* neither token variable set -> **503**; missing/wrong token -> **401**; trailing-slash 307 redirects land on the gated route.
* Approvals: bearer token + tenant ownership (+ approver credential if configured); `decision` is validated; `_approval*` keys are stripped from `edit`; body flags such as `approved: true` are ignored. The signed approval token that operators verify at `/invoke` (`mission/approval.py`) is unchanged.
* Cockpit page: escapes text and attribute contexts, uses `data-*` + one delegated listener (no inline handlers), and asks for the token in an in-page form kept **in memory only** (lost on reload; nothing in web storage).

#### Deployment notes
* **Any deployment that mounts the mission routers must set `AGENTIC_OS_CONTROL_TOKEN`** (and/or `AGENTIC_OS_TENANT_TOKENS`), otherwise mutations and reads answer 503. With reads gated by default the cockpit shell loads but needs a token (or the explicit `AGENTIC_OS_GATE_READS=0` opt-out on a private network) to show data.
* The stack repo's `integrated.compose.yml` `control-plane` service (`agentic_os.control_plane:app`, port 8091, profile `v3`, legacy) mounts these routers via `service.mount` without a token: set `AGENTIC_OS_CONTROL_TOKEN` there before enabling the profile.
* `deploy/k3s/mission-demo/am_service.py` (stack repo) imports the same routers with CORS `*` and defines its own `/book/run` and `/missions/{mid}/stream` routes outside this gate: put them behind `mission_gate` (or the same bearer check) and drop wildcard CORS - tracked as a follow-up, not covered by this repo's PR.
* `deploy/sidekick-devops/sidekick_server.py` inherits the gate; set the token env on that service.
* The enterprise fork (`agentic_os_enterprise/mission/api.py`) is patched separately.

### Projects API CORS
Same-origin only by default (the bundled UI is served by the API itself). Opt in to cross-origin callers with `PROJECTS_CORS_ORIGINS=https://a.example,https://b.example`.

### Operator idempotency cache
`Operator.invoke` dedupes on `(capability, inputs, Idempotency-Key)` (exactly-once for successful side effects).
* Failure-shaped results (`mission.verify.result_failed`) and raised exceptions are **not** cached, so a retry after the backend recovers re-executes. A side-effect-free staged `pending_approval` result is cached; the approved retry carries `_approval` and so has a different key.
* Bounded LRU: `AGENTIC_OS_IDEMPOTENCY_MAX` (default 10000).
* **Per-process.** Not shared between replicas.

### Failure classifier (N4)
One classifier (`result_failed`) serves the verifier, marker synthesis and the cache. Beyond status/`ok`/`error`/HTTP-code shapes it flags: failed nested `results[]`/`items[]` elements, `failed_count > 0`, non-empty `failures`, string status codes (`"500"`), HTTP-style `{detail: "Forbidden"}` bodies, `verified: false`, and an empty `{}` from a side-effecting capability that declares no outputs.

### Multiple replicas
`ReplayGuard` (approval-token nonces) and the idempotency cache are in-memory and per-process. `agentic_os.mission.approval.SharedStore` is the protocol seam for a shared backend (e.g. Redis); none is shipped. Set `AGENTIC_OS_REPLICAS=<n>`; with n>1 a WARNING is logged at startup. Run one replica until a shared store exists.

### Projects API `POST /api/outcomes` (learning evidence)
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

## Security profile and the operator authority boundary

Authentication says who the caller is. It does not say what that caller may do. This page describes how the platform
keeps those apart.

### Profiles (`AGENTIC_OS_PROFILE`)

| Profile | Meaning |
|---|---|
| `production` | A durable authority store, guard and ledger are required; the mission runtime, gateway and Fleet refuse to start without one. Every mission node is checked against the owner's envelope. Operators refuse a side-effecting call that has no approval or execution authorization, and require a durable, shared replay store. |
| `development` | Permissive local mode. Explicit and logged. |
| `test` | Injected guards and clocks; durability not required. |

An **unset** profile behaves as `development` and logs a warning (once per component), so an install without an envelope
database keeps working. `AGENTIC_OS_REQUIRE_AUTHORITY=1` is kept as an alias for `production`. Any other value is an error.
Official production deployments must set `AGENTIC_OS_PROFILE=production` explicitly; the Compose file passes the variable
through to every service that decides authority (empty means development).

**See the posture:** `GET /health` on the control plane and the suite server include `security`; every operator serves
`GET /_security`. A deployment that is in development mode, or whose guard or replay store is not durable, shows
`"ok": false` with the reasons: wire that into readiness checks and Edge Sentinel.

**Check before rollout:** `python -m agentic_os.mission.profile --require-production` exits 1 and lists every missing
requirement (profile, `AGENTIC_OS_AUTHORITY_DB`, `AGENTIC_OS_REPLAY_DB`, a signing secret of 32+ characters, an operator
token, no anonymous operator access).

### The operator boundary

In production an operator's `POST /invoke` runs a **side-effecting** capability only if the call carries one of:

1. a **human approval token** (`approval.py`): unchanged, bound to operator, capability, mission, node and inputs, single-use; or
2. an **execution authorization** (`execution_auth.py`): issued only after the owner's envelope and policy were evaluated.

A valid operator or tenant token alone is refused with 403 *before* the handler runs (it neither executes nor stages).
Reads need only authentication.

An execution authorization is bound to: operator, capability, tenant, mission, node, attempt, reservation (lease), envelope
digest, idempotency key and the canonical effective inputs. Change a recipient, an amount, the capability or the key and it no
longer verifies. The mission identity the handler sees comes from the **signed claims**, never from a `mission_id` in the body.
It is short-lived and **single-use**: the nonce is consumed through the replay store, which in production must be durable and
shared between replicas. It is signed with a key derived separately from the approval key, so an authorization can never pass
as an approval or the reverse. An authorization can never satisfy a capability that declares a mandatory approval.

**Issuers (the only two):**
* the mission runtime, at dispatch, only for the one caller that wins the reservation's RESERVED to DISPATCHED transition;
* the direct-action authorizer (`POST /actions/authorize`, mounted when the runtime has a guard), for an authenticated user acting
  without a mission. It evaluates the same envelope and limits, never authorizes a mandatory-approval capability, and leaves the
  reservation RESERVED: the operator performs the dispatch when it consumes the token, so an unused authorization costs nothing
  once it expires and can be swept, and a swept or already-used reservation can never be dispatched.

**After a timeout:** the reservation becomes UNKNOWN and stays counted. Because the dispatch transition can be won only once, no
fresh authorization exists for that attempt, so the effect cannot be retried by minting another token. Reconcile it
(`AuthorityGuard.reconcile(lease, executed)`) after an independent check.

Operators that share the runtime's `AGENTIC_OS_AUTHORITY_DB` settle (or mark UNKNOWN) the reservation behind an authorization they
consumed; required for the direct-action flow, redundant with the runtime for mission calls.

### The apps' own REST routes

Every state-changing route an app serves declares a policy in `apps/<app>/route_policy.py`; one middleware
(`agentic_os/app_kit/route_authz.py`) enforces it. In production an unauthorised call is refused before the handler runs and a write
request matching no policy is refused.

| Class | Meaning |
|---|---|
| `read_only` | a POST used for a query |
| `public_intake` | anonymous by design, with a written rationale (the privacy request forms) |
| `stages_only` | the core stages unless an approval marker is injected; the marker comes only from an authenticated context |
| `authority_required` | a human holding the permission, an agent authorization, or a service capability |
| `human_only` | a person holding the permission: agents and services are refused |
| `operator_boundary` | the operator's `/invoke` (enforced by the operator) |

Contexts: **human** (operator credential, admin token, or a tenant token that holds the route's permission; a tenant token alone is not enough),
**agent** (`X-Execution-Authorization`, bound to app, capability, tenant, idempotency key and the exact request; single-use), **service**
(`X-Service-Token` with explicit capabilities). A request carrying an authorization is an agent request and gets no human privileges from any other header.
On a multiplexed `/agent/run` route an agent earns an approval marker only for actions listed in the route's `agent_actions` (none by default).

`approval_required` is enforced on every operator `/invoke` in every profile: without a verified human approval the handler does not run
(`AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE=1` is a non-production testing override).

Adding a write route without a policy fails CI. See `audit/operator_capability_inventory.md` (generated by `scripts/audit_operator_capabilities.py`)
and findings F20 to F22 in `audit/AUTONOMY_AUTHORITY_AUDIT.md`, including what this does NOT cover (the kernel services, and network egress).

### The network boundary

Authorization decides who may do what through our APIs; it cannot constrain a process that opens its own socket or shells out. The apps stack can
run behind an allowlist egress proxy on an internal network (`deploy/apps/compose.egress.yml`, or `deploy/k8s/networkpolicy-egress.yaml`):
no route out except the proxy, which denies by default, refuses private/loopback/metadata destinations unless a rule says `private`, connects only to
the address it checked, and logs every decision. It is **opt-in** (the default install is unchanged), its allowlist is **global**, and it does not
inspect payloads. `profile.status()` reports `egress_proxy_configured` as an advisory, and `--require-egress` makes the production preflight fail without one.
Full guarantees and limits: `deploy/egress/README.md`.

### Unknown outcomes: park, reconcile, resume

When a dispatched node's outcome is unknown (the process died, or a side-effecting call errored after it may have been sent), the mission is parked in
`WAITING_RECONCILIATION`, durably, and nothing more is dispatched. A person with an approver or admin credential reconciles it with evidence:

```
POST /missions/{id}/reconcile
{"node_id": "...", "outcome": "not_executed" | "executed", "evidence": {"kind": "provider_record", "reference": "..."},
 "reason": "...", "result": {...only when later steps read what this node produced...}}
```

`not_executed` releases the reservation and the same mission re-runs the node once; `executed` settles it and marks the node done from the evidence. Completed work is never
rerun; a requester credential can never reconcile; the actor is the credential; the evidence and reason are on the mission log. See `audit/AUTONOMY_AUTHORITY_AUDIT.md` step 8.

### The reconciliation supervisor

A parked unknown outcome is probed on a durable schedule by `ReconciliationSupervisor`: only the read-only probe repeats (bounded, with backoff), the business action never does while the
outcome is unknown, and exhausting the budget escalates to a person instead of resending. Only positive execution evidence or an authoritative negative resolves it; a generic 404 or any
non-authoritative "not found" is not observed, not confirmed absent. Independent branches of the mission keep running; only the node and its dependents wait. How to write a probe and what
"authoritative" requires: `docs/RECONCILIATION_PROBES.md`. No provider adapters ship yet, so today every unknown outcome escalates to a person.

### Hard limits and reconciliation

The serial mission loop enforces `deadline`, `wall_clock_s` (active run time of one driver segment), `usd` and `tokens`: a reached ceiling stops the mission with a
`HardLimitReached` event and a reason. A reservation that may already have produced an effect is never sent again: the runtime refuses a second dispatch in every
profile, and `mission/reconcile.py` settles or releases it only when an independent probe proves the outcome, otherwise it stays counted and is flagged for a person
(`python -m agentic_os.mission.reconcile list|run|resolve`). No provider probes ship yet, so today unknown outcomes need a person.

## Signed approval tokens (HTTP operators)

**Problem.** The runtime marks a resumed, human-approved node with `_approval` so a gated handler executes
instead of re-staging. Over HTTP, `Operator.router()` `/invoke` must strip every client-supplied `_approval*`
key (forgery defence, #398), which also stripped the legitimate marker: missions went green with no side effect.

**Design.** `HTTPOperatorClient` removes the marker from `inputs`, mints an HMAC-signed token and sends it as the
top-level body field `approval_token`. `/invoke` strips all `_approval*` keys (case-insensitive, any leading
underscores, recursive through dicts/lists), then verifies the token and injects `inputs["_approval"]` only
from a valid one. Invalid, expired, replayed or mismatched tokens are ignored (handler stages; never a 500).
In-process (`LocalOperatorClient`) is unchanged: the marker travels in inputs, no token.

**Token.** `b64url(payload) . b64url(HMAC-SHA256(secret, payload))`; payload is canonical JSON binding operator,
capability, mission_id, node_id, `inputs_sha256`, the marker, `exp`, random `nonce`. The inputs hash excludes
`_approval*`, `_mission`, `_goal`, `_node` (top level), so an approval for INV-1 $10 cannot be reused for
INV-2 or $999. Comparison is constant-time.

**Config.** (Auto-generated and stored per platform: see [SECRETS.md](SECRETS.md).) `AGENTIC_OS_APPROVAL_SECRET` (read at call time) must be set to the same value on the runtime
**and** on every operator container. Unset/empty: minting returns None, verification always fails; approved
actions stay staged (fail closed) and the client logs one WARNING. TTL defaults to 300 s.

**Replay.** Each operator keeps a bounded in-memory `ReplayGuard` of used nonces (until expiry); a captured
token cannot be reused within its TTL. It is per-process: multi-replica operators need a shared store
(e.g. Redis `SET nonce NX EX`) behind `ReplayGuard.consume` (`Operator.replay_guard`). A full guard fails closed.

**Idempotency.** `Operator._seen` is keyed on (capability, canonical inputs, key); `_approval` is part of the
inputs, so an approved retry is not served a cached staged result. Identical retries still dedupe.

**Not covered.**
- Per-handler gating: apps must still stage unless `_approval` (no generic block of `approval_required`).
- `/agent/run` and other app endpoints that bypass `/invoke` (handled in apps).
- Unauthenticated read capabilities on `/invoke`/`/capabilities`; this adds no caller authentication (operator `/invoke` itself is bearer-authenticated, see README).
- Secret rotation, and a leaked secret (it can mint any approval); a token is not bound to a caller identity.

### Mission / node binding semantics (fail closed)

The signed payload carries `mission_id` and `node_id`. The verifier decides strictness from the **signed payload**,
never from what the caller sends:

- A dimension that is **non-null in the payload is bound**. The `/invoke` body must present a non-empty value
  equal to it (constant-time compare, normalised to `str`). A missing, `null`, empty or whitespace-only value is a
  mismatch, so the call stays staged (`pending_approval`). Omitting `mission_id`/`node_id` can no longer skip the check.
- A dimension that is **null in the payload is unbound** (the minter had no mission/node, e.g. a direct operator
  call outside a mission). Any presented value, or none, is accepted for that dimension. Blank ids are normalised to
  null at mint time. An unbound token is valid for any mission/node and is only as safe as its other bindings
  (operator, capability, input hash, 300s expiry, single use); a verifier cannot distinguish "omitted by a caller"
  from "legitimately unbound", which is exactly why binding is read from the signed payload.
- Only the `mission_id` / `node_id` fields of the JSON body are consulted. `inputs._mission`, `inputs._node` and
  HTTP headers are never trusted for binding.
- Backward compatible: tokens minted by 0.3.29 already carry these fields, so they verify unchanged until expiry.
  No new payload field is required. For approved nodes the runtime sets `_mission` and `_node`, and `HTTPOperatorClient` sends both as `mission_id` / `node_id` (`_node` is set only when an approval exists). Node inputs can never override the reserved runtime keys `_mission`, `_node`, `_goal` (ignored with a warning), and the client refuses to mint a token when an approval is present but `_mission` is missing or blank (the call stays staged), so the runtime cannot mint an unbound-mission token by accident. Non-string ids are coerced to `str`.
- Replay protection and idempotency are unchanged.

### Known limitation: per-process replay/idempotency state

`ReplayGuard` (single-use nonces) and the Operator idempotency cache are in-memory and **per process**. With more
than one replica a replayed token or retried idempotency key can land on another replica and be accepted or
re-executed. Run a **single replica** per operator (or sticky-route by operator) until a shared store implementing
`approval.SharedStore` is wired in; set `AGENTIC_OS_REPLICAS` so a warning is logged if that is violated.

### Saga compensation (rollback) and gated undo capabilities

An undo capability can itself be approval-gated (e.g. `unblock_ip`, `cancel_subscription`, `destroy_delta`).
`Executor.compensate` therefore invokes the undo with a runtime `_approval` marker (`{"approved": True,
"compensation": True}`) plus `_node`/`_mission`: an undo is part of the scope of the action it reverses, and
before handlers were gated these undos ran unconditionally. Over HTTP the client mints the signed token from
that marker like any other approval, so it needs the same `AGENTIC_OS_APPROVAL_SECRET`. Without the secret the
undo stages (fails closed) and the `NodeCompensationFailed` event carries the staged result.

Compensation reporting (formerly a known gap, now closed): `MissionRuntime._saga` records `NodeCompensated`
only when the undo actually succeeded. An undo that staged (`pending_approval` / `executed: False`), errored,
or raised is recorded as `NodeCompensationFailed` (with a `reason` and the raw result), the mission's
`outcome` carries `compensation_incomplete: true` plus `compensation_remaining` (the nodes whose side effects
remain), and `explain()` surfaces both. The saga stays best-effort and never raises past its caller.

#### Compensation authorization

> Policy, events, verification and `requires_approval`: see [COMPENSATION.md](COMPENSATION.md).

`Executor.compensate` is fail-closed. (For capabilities that declare `requires_approval`, the compensation needs its own human approval and a token bound to the undo capability: see COMPENSATION.md section 4.) The compensation marker `{"approved": True, "compensation": True}` is
minted only when ALL hold:

1. **The forward step demonstrably executed in THIS mission**: non-blank mission id, matching node id, executed
   capability and idempotency key; the logged `NodeSucceeded` result (or ledgered result) is not staged, not
   failure-shaped, and not `executed: False`. Evidence is the durable event log through the `evidence_probe`
   the runtime wires, so a restart (new executor, same log) still compensates. A staged preview that an
   approval marker let reach `NodeSucceeded` does not count.
2. **The undo is the registered one**: `node.undo` equals the capability registry's declared undo for the
   forward capability (`undo_registry`, wired by the runtime). A planner-attached undo is refused with
   `undo_not_registered`; with no registry wired no marker is ever minted.
3. Undo inputs (`result`) come from the logged/ledgered forward result, never from plan node fields.

Otherwise no operator call is made and `{"compensation_skipped": True, "undo_error": "<reason>"}` is returned
(`forward_not_executed` / `undo_not_registered`); the saga records `NodeCompensationFailed` and
`compensation_incomplete`. Under these conditions a forward step admitted by policy without a human gate
(charge -> approval-gated refund) is rolled back with the marker; a human `ApprovalGranted` is recorded
(`approved`) but is not required. `ApprovalInvalidated` does not block undoing a step that did execute; it never
authorises undoing one that did not. Double compensation is idempotent (also across restarts via
`NodeCompensated`). Client-supplied `_approval*` is never consulted.

Limits operators/humans must know:
- Multi-replica runtimes, or a crash between the undo call and `NodeCompensated` being persisted, can call the
  undo twice: operators MUST honour the attempt-scoped `{idempotency_key}:undo:{attempt}` key.
- A forward success that crashes before `NodeSucceeded` is persisted gets NO undo and NO marker (fail-closed);
  the effect is left behind and must be reconciled by an operator/human.
- The in-memory ledger is a best-effort fallback for executors without a probe, bounded (LRU 4096): after
  eviction (or a process restart) a completed step is NOT undone (fail-closed: `compensation_skipped` +
  `NodeCompensationFailed` / `compensation_incomplete`). The probe path uses the durable log and has no such limit.
- "Executed" is judged by the strict `verify.proves_execution`: empty `{}`, truthy `staged`, status in
  pending_approval / awaiting_approval / awaiting_connector / staged / held / pending / queued (exact matches; other `awaiting_*` strings are not rejected), `executed: False`, failure shapes, or any of
  these nested under `result` / `results` / `items` all mean NOT executed (the commit-path classifiers are unchanged).
- Production wiring: `MissionRuntime._executor_for` wires the probe and registry for every executor the runtime
  uses. A custom executor that bypasses it has no registry, so its undo runs WITHOUT the marker (a gated undo
  stages visibly).
- One executor serves one runtime; binding a second runtime's event log raises.

The Go port (`go/mission`) does not mirror this: its `Compensate` never mints an approval marker (it sends only
`undo_of`/`result`), so there is no unconditional-approval path to guard.

## Permissions plane — access-control admin

Fine-grained data access for every app: grant a **subject** (app / role / user) read/write on a
**resource** (database / table / corpus / tool), sliced by **row scope** and **column mask**. It is a
kernel plane (a control-plane primitive alongside Approvals), not a business app, so it deploys with
the core stack — no business apps required.

- **UI:** `/permissions` on the control plane (linked from a *Permissions* tile on `/overview`).
- **Code:** `agentic_os/permissions.py` (grant store + plane + preview + enforcer), wired into
  `agentic_os/control_plane.py` (routes) and `agentic_os/views.py` (page).

### Model

| Piece | What it is |
|---|---|
| `Grant` | `<subject_kind:subject>` may `<actions>` on `<resource_kind:resource_name>`, with `row_scope` (`all`/`own`/`in`) + `masked_columns`. A grant on `crm` covers `crm.customers` (dotted prefix). |
| `GrantStore` | Encrypted-at-rest JSONL CRUD (`grants.enc`), atomic writes, **fail-closed**. |
| `PermissionsPlane` | `authorize(identity, resource, action) → AccessDecision` (privileged bypass → resource grant → row scope → column mask); `slice()` applies it. |
| `make_authorizer(store)` | The **enforce** side a client app installs so grants gate every tool call. |

`authorize` combines matching grants: rows = most-permissive (`all` > `own` > `in`); a column is
masked only if **every** matching grant masks it (an added grant can unmask).

### Encryption at rest + fail-closed

Grants are stored as `grants.enc`, encrypted with **AES-GCM** (AEAD = confidentiality **and**
tamper-evidence in one). Any hand-edit of the file fails authentication on read → the store goes
**sealed** → the plane honors **no grants and denies everything, including the admin bypass**.
`add`/`remove` refuse to overwrite a sealed file.

Key precedence: **`PERMISSIONS_KEY`** (base64 16/24/32 bytes, from Vault/env) → a generated on-box key
file (`.permissions.key`, dev only). Status (`GET /api/permissions/status`) reports
`encrypted` / `key_source` (`env`|`keyfile`) / `sealed` / `count`; the UI shows a badge.

**Honest ceiling (on-host model):** a root user who can read the running key (env/`/proc`) can still
forge grants — encryption stops *file* tampering, not a fully-compromised host. Keep `PERMISSIONS_KEY`
in Vault so the key isn't on the data volume. For a *root-can't-forge* posture, move to an asymmetric
verify-only design (server holds only the public key; signing happens off-box).

### API auth (write path)

`POST`/`DELETE /api/permissions/grants` are gated by **`PERMISSIONS_ADMIN_KEY`** (its own key, so it
is independent of the fleet-ops `AGENTIC_OS_API_KEY`; falls back to it if unset). Reads/preview are
open. The UI has an admin-key field (stored in `localStorage`, sent as `X-API-Key`; HTTP Basic with the
key as password is also accepted; compared with `hmac.compare_digest`). This is a **separate layer**
from at-rest encryption: it protects the network write path, not the file.

#### Public demo: simulated writes (fail closed)

The standalone showcase (`deploy/permissions-showcase-demo`, demo.redevops.io/permissions) stays
anonymous and interactive but visitors can never mutate the shared, persistent store. **An unset key is
never "open"**: with no key configured, or a missing/invalid key on the request, grant writes are
*simulated* in an in-memory per-session overlay (`agentic_os/permissions_overlay.py`):

- keyed by the `perm_demo_sid` cookie (HttpOnly, SameSite=Lax); responses carry `X-Demo-Simulated: 1`
  and the normal success shape, so the page works unchanged;
- never persisted, invisible to other visitors; `GET grants`, `status`, `preview` and `authorize`
  evaluate against base grants + that session's overlay;
- capped (25 simulated adds / 50 removals per session, 500 sessions, oldest evicted) and expires after
  30 minutes idle; over-cap writes return 429;
- a valid admin key persists writes exactly as before. At-rest encryption is unchanged.

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/permissions` | — | Admin page (grant editor + live preview) |
| GET | `/api/permissions/status` | — | encrypted / key_source / sealed / count |
| GET | `/api/permissions/grants` | — | list grants |
| POST | `/api/permissions/grants` | `PERMISSIONS_ADMIN_KEY` | add/replace a grant |
| DELETE | `/api/permissions/grants/{id}` | `PERMISSIONS_ADMIN_KEY` | remove a grant |
| POST | `/api/permissions/preview` | — | permissionless vs permissioned over the sample resource |
| POST | `/api/permissions/authorize` | — | dry-run a decision |

### Enforcing grants in a client app

The control plane is the **write** side; a client app is the **read/enforce** side. Both share the
grants file (`PERMISSIONS_DIR`, default `/data/agentic-os/permissions`), mounted into both containers.
The app installs the authorizer once:

```python
from context_runtime.tools import set_default_authorizer
from agentic_os.permissions import GrantStore, make_authorizer

store = GrantStore("/data/agentic-os/permissions")   # same dir as the control plane
set_default_authorizer(make_authorizer(
    store,
    app_resources={"support": ("table", "crm.customers")},   # app → its own resource
    tool_resources={"read_customers": ("table", "crm.customers")},  # optional per-tool overrides
))
```

Every gated tool call is then authorized against the plane; a saved grant takes effect on the next
request. Tools that touch no controlled resource pass through.

### Deploy & secrets

The plane reads two keys from the environment — `PERMISSIONS_KEY` (encryption) and
`PERMISSIONS_ADMIN_KEY` (write authorization). Source them however your deployment manages secrets
(a secrets manager, a `.env`, CI variables). Flow:

```
secret source → PERMISSIONS_KEY / PERMISSIONS_ADMIN_KEY in the control-plane environment
  → GrantStore encrypts grants.enc with the key; the write API is gated by the admin key
```

The grants file persists on the control-plane data volume.

#### Key rotation

- **Admin key** — safe, no data impact: set a new `PERMISSIONS_ADMIN_KEY` and restart the
  control-plane.
- **Encryption key (`PERMISSIONS_KEY`)** — CAUTION: the existing `grants.enc` is encrypted with the
  old key, so rotating the key alone makes it undecryptable → sealed (all access denied,
  fail-closed). Rotate by resetting the store (delete `grants.enc` and re-seed grants) after setting
  the new key. A rekey-in-place step (decrypt-with-old → re-encrypt-with-new, preserving grants) is
  not yet implemented.

## Gate notifications

Pushes a parked human-approval gate to people through the existing `channels.py` channels, durably and
without pretending to be exactly-once. Implemented in `agentic_os/mission/gate_notify.py`. No new dependency
or service (stdlib SQLite for the outbox).

Scope: wired today only into the demo `unified_server.py`. The production mission API is NOT covered yet;
its host calls `gate_notify.install_from_env(runtime)` (idempotent per runtime) once, e.g. from the default-deny
API work (PR #444). This PR does not edit `api.py`, `service.py`, `api_gate.py` or `control_auth.py`.

### Enabling (default OFF)
Nothing is installed unless `AGENTIC_OS_GATE_NOTIFY=1` **and** a recipient on a configured channel exists.
- `AGENTIC_OS_GATE_NOTIFY_RECIPIENTS="alice:slack:C123,bob:telegram:555"` (`id:channel[:address]`)
- `AGENTIC_OS_GATE_NOTIFY_ESCALATE_TO="boss:slack:C999"`
- `AGENTIC_OS_GATE_NOTIFY_LINK_BASE=https://console/needs-you`
- or `AGENTIC_OS_GATE_NOTIFY_CONFIG=/path.json` with `recipients`, `escalation`, `link_base`, `outbox` (sqlite path),
  `reminder_after` (900s), `escalate_after` (1800s), `expire_after` (86400s), `backoff_base`/`backoff_cap`,
  `max_attempts` (8), `rate_max`/`rate_per` (5 per 60s per recipient).
- `AGENTIC_OS_GATE_NOTIFY_DB` = durable outbox file (default `$AGENTIC_OS_STATE_DIR` or `~/.agentic-os`, `gate_notify.db`). A `:memory:` outbox is refused with a warning, so reminders, expiry state and callback replay protection survive restarts.
- Channel credentials stay in the existing `AGENTIC_OS_*` channel env vars.

### Flow
`on_gate_reached` (LifecycleContributor) enqueues one outbox row per recipient. It ignores `GateReached`
fired for policy-denied blocks (no human can act). `tick(now)` (and the optional daemon thread, `start()`)
sends due rows, retries with exponential backoff to a cap, enqueues ONE reminder (skipped if any notice was
acknowledged), ONE escalation to the secondary recipients, and after the TTL appends a durable
`GateExpired` event plus an "expired" notice.

### Guarantees, stated honestly
- Transport is **at-least-once**; processing is **idempotent**. Key = sha256(mission, node, gate_version,
  recipient, channel, kind); `gate_version` = seq of the `NodeParked` event. Duplicate hook calls, ticks and
  restarts never create a second outbox row, and a row with a recorded receipt is never re-sent.
- **Duplicate external messages are still possible**: if a provider delivers but the ack is lost (timeout),
  the retry sends again. The harness measures this (see below). We do not promise zero duplicates.
- Concurrency: `tick()` is serialized per process and each due row is claimed with an atomic compare-and-swap in SQLite, so threads, daemon+manual ticks and multiple processes/workers on one file send a row once (tested with threads and 4 processes). Duplicates arise only from lost acks and the crash window below.
- A crash between a successful send and the receipt write leaves the row retryable (same duplicate risk).
- Retry classification: timeouts/network, 408/425/429, 5xx and Slack `ratelimited` are retried; other 4xx and
  provider rejections are permanent (`failed`). Errors are redacted; tokens are never logged or stored.

### Approval is never granted by a notice
Notices contain capability, mission id and a link to the authenticated console. They never contain the
signed approval token or any secret, and text says replying does not approve. Nothing parses chat replies.
Approval happens only through the existing authenticated control path (`control_auth`, `runtime.approve`).

### Expiry
`GateExpired {node_id, gate_version, expired_at, auto_decision: null}` is appended to the mission log. It is
not a decision: the mission stays `waiting_human`, `NodeParked` remains open, and `approve` still works.
`MissionRepository.pending_human` adds `expired: true` (a read-only fold); other folds ignore the event.

### Receipts and Needs You
`GET /api/needs-you` items gain `expired` and `notices[]` (kind, recipient, channel, state
`pending|retrying|delivered|failed|cancelled|acknowledged`, attempts, provider_msg_id). No addresses or text.

### Inbound callbacks (acknowledge only)
Every notice carries an opaque, random per-notice ack reference (`ack=<ref>` in the link, or `ack=<ref>` in the
text when no link base is set). It is not an approval token, is not derived from one, and is never shown in
`/api/needs-you`. A relay (e.g. a chat-platform bot you operate) reads `ref` from the delivered message and POSTs
`{"action":"ack","ref":"<ref>"}` to `POST /api/gate-notify/callback` with `Content-Type: application/json`,
`X-AOS-Timestamp` (plain decimal epoch seconds) and `X-AOS-Signature` = hex HMAC-SHA256 of `v0:<ts>:<raw body>`
using `AGENTIC_OS_GATE_NOTIFY_CALLBACK_SECRET` (503 until set). 5-minute window, bodies over 16 KB refused,
signatures single-use and persisted (consumed only after full validation). Any other action (approve/reject)
is refused. This route is deliberately public-by-signature (no bearer) and can only ever record `ack`.
It is a relay-signed scheme, not a native Slack/Teams verifier.

### Channel fixes
Twilio now form-encodes; 10 s timeouts; `send()` returns a `SendResult` (ok/status/message_id/permanent);
the legacy approval text no longer promises "reply: approve <id>".

### Other notes
The DB directory is created 0700 and the DB (and -wal/-shm) 0600; a pre-existing broader directory triggers a warning. If the outbox cannot be opened (read-only dir, unsupported WAL filesystem) `install_from_env` warns and installs nothing; missions are unaffected.
Growth: the DB grows with long-open gates (rows are kept until resolved). `prune()` runs at the end of every `tick()` (the background thread, every 5 s by default) and removes resolved gates older than `retention` plus stale rate-limit samples; it can also be called directly.

`time_to_decision()` is computed from the NodeParked and NodeResumed/NodeSkipped event timestamps (no tick
needed; superseded and open gates excluded). Resolved gates and their rows are pruned after `retention` (7 days);
unresolved rows are kept. Ids/capability in notice text are sanitized (no `@`, `<>`, markup) and URL-encoded in links.

### Metrics harness (`tests/test_gate_notify.py::test_harness_er07_targets`)
Fake transport + fake clock. Healthy transport: 20/20 gates delivered within 60 s. Duplicate hook calls x3 plus
repeated ticks: exactly 1 outbox row. External sends under 0/1/2 injected lost-acks: 1/2/3 (duplicates track
ambiguous acks). Forged/unsigned/stale/replayed callbacks accepted: 0. Old pending gate: exactly one reminder,
one escalation, one `GateExpired`, zero approval events. `time_to_decision()` reports per-gate seconds.
