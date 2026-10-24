# Autonomy and authority audit (read-only phase) — 2026-10-23

Scope: the execution, delegation and authority paths in `agentic_os` at `main` (commit 7888a64).
Method: direct reading and grep by the author; three helper agents were started; two had no shell and produced partial reads, the third (loops/delegation) produced a full report. Items marked "(re-read)" were re-verified by the author; items marked "(reported)" are the helper's claim and are not independently verified. Tags: VERIFIED (code read), INFERRED, NOT_INSPECTED.
This is not a complete audit: see "Not inspected".

## What already exists (build on it, do not replace it)
| Primitive | Where | Status |
|---|---|---|
| Gateway pipeline: visibility, budget, approval, egress, audit, idempotency | `agent_gateway/gateway.py:126-226` | VERIFIED |
| Versioned, digest-pinned policy; deny > require_approval > allow; actionable denials | `mission/policy.py` | VERIFIED |
| Signed, input-bound, single-use approvals; HTTP `/invoke` strips forged `_approval*` and verifies a token | `mission/operator_sdk.py:266-300`, `mission/approval.py` | VERIFIED |
| Tenant spend cap applied to a mission's budget | `mission/runtime.py:1633-1638`, `control_auth.py` | VERIFIED |
| Concurrency keys, `max_parallelism`, resource scheduler | `mission/concurrency.py`, `resource_scheduler.py` | VERIFIED |
| Process sandbox: scrubbed env, rlimits | `mission/membrane.py`, `agent_gateway/sandbox.py` | VERIFIED (not exercised here) |
| Mission `Budget` (usd, tokens, human_minutes, wall_clock_s) | `mission/types.py:69` | VERIFIED |

## Findings
| # | Finding | Evidence | Severity |
|---|---|---|---|
| F1 | A delegating agent supplies its own mission `constraints` (`request.arguments["constraints"]`), including none. The mission's governance is chosen by the party being governed. Tenant limits clamp spend only. | `gateway.py:~170` (`constraints = tuple(request.arguments.get("constraints") or ())`), `mission_adapter.py:35-48` | **High** |
| F2 | Gateway defaults are open: `_AllowAllEgress`, `_AllowAllBudget`, `_NoApprovals`; `IF_POLICY` writes auto-run when no `policy_allows` is injected ("open-core default"); MISSION delegation is never approval-gated at the gateway. | `gateway.py:57-80, 228-231, 142-160` | **High** (by design for open-core; unsafe as a production default) |
| F3 | `MissionPolicy` cannot express resources, parameter limits, rate/amount limits, time windows, expiry, delegation, or standing grants. Rules match only a few selectors, `cap:<name>` or a spend threshold. | `policy.py:46-78` | High (capability gap) |
| F4 | The `ALLOW` effect never overrides a gate: it ranks lowest and the highest-ranked match wins, but the module comment says allow "overrides a broader gate". Behaviour and comment disagree; standing grants need a defined meaning. | `policy.py:41, 123-130 (rank), evaluate` | Medium |
| F5 | No capability attenuation for child work: no spawn/child-token concept in the mission or gateway code. "child" hits are trace spans and sandbox processes. | grep of `mission/`, `agent_gateway/`, `sidekick/`, `projects/` | High (nothing to attenuate yet) |
| F6 | `Budget.wall_clock_s` is recorded and copied but no enforcement site was found; no progress watchdog, loop controller or cancellation propagation found. | grep `wall_clock_s` (only types/record/copy sites) | High for long-running loops |
| F7 | `OwnerOnlyAuthority.authorize` ignores `capability`; `resolve_for_node` returns a decision the caller must honour. | `execution_context.py:127-173` | Medium (OSS default; callers not all checked) |
| F8 | `safety.scan_action` is a regex blacklist that only produces an advisory badge. | `safety.py`, `context.py:56-58` | Low (not an enforcement point) |
| F9 | `gated()` accepts any truthy `_approval`. HTTP `/invoke` is protected by stripping + signed-token verify; in-process callers can still set it. | `approval_gate.py:18`, `operator_sdk.py:282-287` | Low-Medium |
| F10 | Deploy: one flat `apps` network, no egress restriction in `deploy/apps/compose.yml`; no non-root user, cap_drop or read-only fs seen there. Raw outbound HTTP (`urllib`) appears in ~60 modules, including `router.py`, `sidekick_assistant.py`, `mission/util.py`. | grep; `compose.yml` | Medium-High for bypass resistance |
| F12 | No runtime spend enforcement in the serial mission loop: `Budget` is checked at plan time (`over_budget`) and, at runtime, only inside fork/join (atomic reserve-and-check of usd and tokens before each branch). `tokens` and `wall_clock_s` are not checked at plan time. `Mission.deadline` is consumed only by fork/join. | `runtime.py:530-533`, `fork_join.py:963-975`; grep `wall_clock_s`, `deadline` | **High** for long-running loops |
| F13 | No child-agent concept: single-mission DAG engine, serial by default (`max_concurrency=1`); the only fan-out is fork/join whose branches are single capability calls. No recursion, no depth or fan-out ceiling beyond fork limits (16 branches, 4 concurrent, 4 open forks). `topology.py` labels (coordinator_workers, voting, ...) appear advisory only. | `runtime.py:701-717`, `fork_join.py:119-121` (reported; limits INFERRED from the report), grep `child|parent_mission|max_depth` found none | High (design gap) |
| F14 | Default executor is process-wide with no per-child credential scoping; scoped leases exist only if `broker`, `authority`, `authority_for` are wired (enterprise path). Fork branches are limited to their declared capability, which must be within the mission's grants, rechecked at start. | `executor.py:111-119, 231-248`, `fork_join.py:655-663` (reported) | High (open core) |
| F15 | `Fleet.dispatch(background=True)` runs a daemon thread per job with global router credentials, no timeout, no cancel, no TTL, results in an in-memory dict. It sits outside any envelope. | `fleet.py:111-131` (re-read) | High |
| F16 | No progress watchdog or in-flight cancel. A hung operator call blocks the serial loop; a running fork branch cannot be killed (late results kept as receipts). `reliability.py` is an evaluator not wired into the loop. No dead-letter queue (`COMPENSATION_FAILED` and quarantine only). | `runtime.py:711-718` (re-read), reported for the rest | High |
| F17 | UNKNOWN-outcome handling is fork-only (`UNKNOWN_EFFECT`). The serial path has no timeout or UNKNOWN state, so an unknown outcome is reported as a node failure and the saga runs. Idempotency stores (operator, executor, gateway) are per-process, not shared across replicas; the gateway store caches successes only. | `fork_join.py:1320-1323`, `operator_sdk.py:15`, `gateway.py:132, 223` | High for multi-replica |
| F18 | Approval TTL defaults to unlimited (`AGENTIC_OS_APPROVAL_TTL_S` unset); `Router` budget is checked after the model call, so the spend is already incurred. | `runtime.py:99-105` (reported), `router.py:95-97` (re-read) | Medium |
| F11 | Compaction: Sidekick is single-turn (no history); no summarisation pipeline found in the files read. Matches for `compact/summariz` in `mission/store.py`, `runtime.py`, `context_view.py` and others were **not** read. | partial | NOT_INSPECTED |

## Not inspected (do not read this as clean)
`mission/context_view.py` and the other compaction/summary hits; enterprise authority gate and registry; every `apps/*` handler's gating;
`app/` LLM layer and MCP tool-call surfaces; peer-agent messaging (none found, not exhaustively searched); the other deploy files and Dockerfiles;
the ~60 raw-HTTP modules individually (reachability from agent-controlled input).

## Consequence for the design
Most primitives exist. The missing piece is a **durable authority object that the governed party cannot write**, carrying resources, limits,
expiry and delegation, enforced at the gateway **fail-closed**, plus child attenuation and a loop supervisor. First implementation step:
`mission/authority.py` (typed envelope, digest, attenuation, evaluation) and a gateway guard that takes the envelope from trusted state
(not from `request.arguments`).

## Implemented in this branch (step 1 of the plan)
* `agentic_os/mission/authority.py` — `mission-authority/v1`: typed `Grant`s (capability pattern, resource selectors, tier cap, per-action and
  cumulative amount, call count, rate), `AutonomyBudget`, `AuthorityEnvelope` (expiry, revocation epoch, forbidden patterns, required
  constraints, delegation), digest, **monotonic attenuation** for children, pure `evaluate()`, `AuthorityGuard` with atomic counters
  (`MemoryLedger`, durable `SqliteLedger`). Verdicts: ALLOW, ESCALATE (owner can extend: parks as pending approval), DENY (out of scope, with a
  machine-readable amendment).
* `AgentGateway(authority=...)` — fail-closed when configured: no stored, unexpired, unrevoked envelope that covers the call means no
  dispatch (F2). Delegated missions inherit the envelope's `required_constraints` and the child quota; the agent can add constraints but not
  drop the owner's (F1). The envelope is read from trusted state by `tenant:subject` (or `authority_key`), never from request arguments.
* Tests: `tests/test_mission_authority.py` (31), `tests/test_agent_gateway_authority.py` (11). Existing gateway/mission/policy/approval suites
  unchanged (900 passed; `test_agent_gateway_sandbox::test_default_backend_runs_a_capability_in_real_containment` fails identically on `main`).

Deliberately NOT in this step (still open, in plan order): durable envelope store (only in-memory exists), wiring `authority` as the default in
production deployments, loop supervisor with progress watchdog and wall-clock/deadline enforcement in the serial loop (F6, F12, F16), child
identities with scoped credentials (F5, F14), Fleet background jobs inside the envelope (F15), UNKNOWN-outcome handling in the serial path (F17),
egress/sandbox network boundary (F10), compaction/provenance checks (F11, NOT_INSPECTED), Sentinel events, Sidekick UX, business standing-grant pilots.
`MissionPolicy` is unchanged (F3/F4 remain: the envelope covers resources/limits/expiry; ALLOW-vs-gate semantics still need a decision).

## Step 2 (this branch): durable authority, production enforcement, standing-grant semantics
Owner decision 2026-10-23: a standing grant may satisfy an ORDINARY approval gate only when it explicitly covers the action and its parameters;
it never overrides a DENY, never exceeds its limits, and never bypasses a MANDATORY approval. Un-migrated rules keep their behaviour.

| Outcome | Behaviour (as implemented) |
|---|---|
| `DENY` rule | Blocked regardless of any grant (`policy_denied`) |
| Capability/gateway MANDATORY approval | Human approval required, grant ignored |
| Legacy `approval:` constraint, or a rule not marked `standing_grant_satisfies` | Human approval required (today's behaviour) |
| Rule marked `approval_mode: standing_grant_satisfies` + explicit grant within limits | Runs unattended; `StandingGrantApplied` event records envelope digest, grant, policy digest, inputs digest |
| Grant matches but amount/rate/call/budget limit reached | Human gate; `StandingGrantDeclined` with an amendment |
| No matching grant / expired / revoked / wrong tenant / no envelope | Human gate; never a permissive fallback |
| Bare wildcard grant (`*`) | Cannot stand in for an approval (`grant_not_explicit`) |
| Score-based risk gate | Needs the grant's explicit `covers_dynamic_risk`; regulatory or non-permissioned risk is never waived |

* Single source of truth: `MissionPolicy` says whether an action is prohibited or needs special treatment; the envelope says what the owner authorised.
* `PolicyRule.approval_mode` (`per_action_required` default | `standing_grant_satisfies`, REQUIRE_APPROVAL only). It is omitted from the canonical JSON at its
  default, so every existing policy digest is byte-identical (pinned in tests).
* Evaluated per node at dispatch against the exact effective action (resolved inputs, tenant, cost); counters are reserved atomically (verified with 10
  concurrent runtimes sharing one allowance, memory and SQLite), so a parameter change or a lost race falls back to the human gate.
* Gateway: an ordinary `REQUIRED` approval policy is satisfied by an explicit grant the owner raised to that tier (`max_tier`); `MANDATORY` is not; the default
  `max_tier=1` changes nothing.
* `SqliteEnvelopeStore` (versioned, history kept, durable revocation per mission and per tenant epoch) and `SqliteLedger`; `authority_from_env()`;
  `AGENTIC_OS_REQUIRE_AUTHORITY=1` makes `AgentGateway` refuse to start without a durable guard, and `AGENTIC_OS_AUTHORITY_DB` selects the database.
* Not covered yet: fork/join branches still use the human gate (they have their own policy evaluation path); a reservation is not released if the node is
  later cancelled before it runs (conservative: never overspends); the runtime only uses the guard when `runtime.authority` is set.

## Step 3 (this branch): enforcement as an invariant, one gate for every topology, reservation lifecycle

Owner direction 2026-10-23, from the three gaps left by step 2: (1) fork branches must follow the same standing-grant semantics, (2) reservations need an
explicit lifecycle, (3) authority enforcement must be a production invariant with an explicit development opt-out.

**Execution profile** (`mission/profile.py`, `AGENTIC_OS_PROFILE`):
| Profile | Behaviour |
|---|---|
| `production` | A durable store, guard and ledger are required. `MissionRuntime`, `AgentGateway` and `Fleet` refuse to start (and the runtime refuses `run()` if the guard is removed afterwards) without one. Every mission node is checked against the owner's envelope: no covering envelope, no autonomous action. |
| `development` | Permissive local mode, explicit and logged once. |
| `test` | Injected guards and clocks; durability not required. |
Unset behaves as `development` and logs a warning once (so an existing install without an envelope database does not break); `AGENTIC_OS_REQUIRE_AUTHORITY=1`
is an alias for `production`; any other value is an error. **Open decision:** whether an unset profile should become `production` by default.

**One gate** (`MissionRuntime._authority_gate`), used by serial nodes and fork branches: DENY -> mandatory approval -> standing grant -> ordinary approval -> run.
In production a node with no human gate is still checked against the envelope: ALLOW runs (`ScopeAuthorized`), a limit reached parks as a human gate with rule
`authority_scope`, anything else is a hard stop (`authority_denied`). Human-approved nodes are not re-checked (the owner's approval is itself the authority).

**Reservation lifecycle** (`UsageLedger` lease API; memory and SQLite, each transition atomic and idempotent):
`RESERVED -> DISPATCHED -> SETTLED`, plus `RELEASED` and `UNKNOWN`.
| Event | Transition |
|---|---|
| Gate allows | RESERVED (one lease per node attempt: `<mission>:<node>:a<n>`; re-evaluating the gate never recounts) |
| About to dispatch (`NodeDispatched`, `BranchStarted`, gateway handler call, Fleet job start) | DISPATCHED |
| Confirmed success (also a late fork receipt) | SETTLED with actual consumption |
| Failed or refused before dispatch; mission ended or cancelled with it undispatched | RELEASED |
| Staged preview (did not execute), or an error that says `not_applied`, or a non-side-effecting failure | reconciled to RELEASED |
| Side-effecting call errored or timed out | UNKNOWN: stays counted until `reconcile(lease, executed)` |
Time alone never releases a DISPATCHED or UNKNOWN lease; `sweep_undispatched` is an explicit operator action that touches only RESERVED. Lease state is durable on
SQLite, so a restart leaves orphan DISPATCHED/UNKNOWN leases visible through `outstanding()` for reconciliation instead of silently losing them. The mission-runtime
lease map is in memory: after a restart those leases are reconciled by an operator, not by the runtime.

**Bypass check** ("can background jobs, fork branches, raw tool calls or direct handlers skip the gateway?"):
| Path | Result |
|---|---|
| Fork branches | **Closed.** Same gate as serial nodes; lease lifecycle wired; tested serial/fork, production, limits, unknown. |
| Fleet `dispatch` and background jobs | **Closed** (new). `fleet.<module>.<action>` is checked against the envelope in production; background jobs hold a lease for their life; mandatory-approval actions still stage for a human. Fleet `up`/`down` (docker compose, git clone) are admin lifecycle calls and are **not** covered here. |
| Gateway DIRECT and MISSION calls | **Closed.** Lease released if the call never dispatched, settled on success, UNKNOWN on a side-effecting error. |
| **F19 Operator `/invoke` and direct application handlers** | Was: not closed (any caller with a valid operator or tenant token ran the handler; heuristic count said 7 of 53). **Closed for the operator `/invoke` path in step 4 (below).** The heuristic count was wrong: the AST inventory finds 54 side-effecting capabilities, 7 wrapped in `gated()`, 34 declaring `approval_required` (27 of those without `gated()`; only the mission runtime honours the flag), 20 with neither. |
| Raw tool calls / raw outbound HTTP | **Not enforceable in-process.** No agent shell tool was found in the open core, but ~60 modules use `urllib` directly and the compose network is flat (F10). Needs the network boundary (step 6). |

## Step 4: the operator authority boundary (F19) and the security profile
Decisions recorded 2026-10-23 (owner):
1. **Default profile:** keep an unset profile as `development` with a prominent warning (compatibility; `AGENTIC_OS_REQUIRE_AUTHORITY=1` stays an alias). Official production deployments set `AGENTIC_OS_PROFILE=production` explicitly and validate it at deploy time. A later major release may require an explicit profile. The effective profile is exposed in startup logs and readiness (`/health`, operator `/_security`). Considered, not built: binding administrative endpoints to localhost in development.
2. **F19:** authentication is not authority. The operator requires a trusted, per-invocation authorization for consequential calls; the mission identity is derived from the validated authorization, never from the request body; production handlers must validate the same authorization or be unreachable except through the governed operator; token consumption uses durable atomic state; a consumed token followed by a timeout is UNKNOWN and follows the reconciliation lifecycle (a fresh token cannot retry). Direct, non-mission actions get a separate, auditable authorization flow.
3. **Lease recovery:** orphaned dispatched reservations are not released automatically. Designed, not built: a durable lease record (reservation id, mission/node/attempt, dispatch state, execution id, idempotency key, provider reference, reconciliation status) so that after a restart RESERVED with verified non-dispatch releases, DISPATCHED with a confirmed provider result settles, UNKNOWN stays counted until independently reconciled, and unverifiable effects stay parked for a human; a restart must never turn uncertainty into permission to repeat a side effect.

Revised order: F19 (this step) -> network egress enforcement (parallel) -> loop supervisor -> child-agent identities and credentials -> restart recovery and reconciliation (alongside the supervisor) -> context provenance and Edge Sentinel.

**Implemented here:**
* `mission/execution_auth.py`: signed, domain-separated, single-use execution authorization bound to operator, capability, tenant, mission, node, attempt, lease, envelope digest, idempotency key and canonical effective inputs.
* The runtime mints claims only for the caller that wins `lease_dispatch_once` (an atomic RESERVED to DISPATCHED transition on both ledgers); the HTTP operator client turns them into a token. A retry after a timeout or restart gets no claims.
* Operator `/invoke` in production: side-effecting capabilities need a human approval or an authorization, otherwise 403 before the handler runs; claims are verified before anything else; the body `mission_id` is ignored; a mandatory-approval capability can never be satisfied by an authorization; the operator settles or marks UNKNOWN the reservation it consumed (shared authority ledger).
* `mission/action_auth.py` + `POST /actions/authorize`: the direct-action flow (no mission). Reservation stays RESERVED until the operator dispatches it, so unused authorizations are sweepable and a swept one can never run; mandatory-approval capabilities are refused.
* Production requires a durable, shared replay store for operators (`default_replay_guard`).
* `mission/profile.py`: `status()`, startup `announce()`, `check_environment()` and `python -m agentic_os.mission.profile --require-production`; posture in `/health` and `/_security`; `AGENTIC_OS_PROFILE` passed through every agent service in `deploy/apps/compose.yml` (empty means development).
* `scripts/audit_operator_capabilities.py` and the generated `audit/operator_capability_inventory.md`.

**F19 acceptance table (tests/test_operator_authority_boundary.py, 23 tests):** valid operator token without action authority denied; forged `mission_id` denied (claims win); token for another capability denied; changed recipient or amount denied; replay on another replica denied (by nonce consumption for runtime tokens, verified by mutation; by the dispatch-once ledger for direct ones); expired denied; authorization cannot pass as an approval nor the reverse; a tenant cannot use another tenant's authorization; standing grant with matching inputs runs over real HTTP without a human; direct user action runs without a mission and the operator settles it; unused direct authorization sweeps and then cannot run; timeout after a possible side effect is UNKNOWN with no second authorization; human approval still works and is single-use; reads need only authentication; development unchanged.

**F20 (new, High): the apps' own REST routes bypass the operator.** The inventory finds 45 POST/PUT/PATCH/DELETE routes on apps' own surfaces. Those handlers do not pass through `/invoke`, so none of the above applies to them. Needs per-app authorization on those routes or network isolation so that only the governed operator and the UI gateway can reach them. Not built; do not claim these are governed.
**F21 (new, Medium):** `approval_required` is honoured only by the mission runtime (27 declared capabilities have no `gated()`); outside production, direct `/invoke` still runs them.
**Still open:** raw outbound HTTP and shell access (network egress boundary), the loop supervisor, child identities and scoped credentials, the durable lease record and automated restart reconciliation, Fleet `up`/`down`, Edge Sentinel events.

## Step 5: route authorization for the apps' own write routes (F20) and approval consistency (F21)
Owner direction 2026-10-23: do not build 45 separate mechanisms; use one enforcement layer. Three authorization contexts (human direct action, agent
action, internal service action) obtain authority differently and pass the same policy enforcement point. A tenant API key must not confer every permission.
Standing grants must work the same through REST, `/invoke`, serial, fork and Fleet. A new unclassified write route must fail CI. The 45-route inventory is not
the whole surface, so WebSocket commands, background jobs, hooks, webhooks and internal services are included.

**Implemented:**
* `agentic_os/app_kit/route_authz.py`: `RoutePolicy` (capability, access class, permission, `agent_actions`, rationale) and one ASGI middleware per app.
  * **human**: the app's operator credential or the admin token, or a bearer tenant token that must hold the route's permission among the refs that tenant was
    granted (`AGENTIC_OS_TENANT_LIMITS`): a tenant token alone is not enough.
  * **agent**: an execution authorization (`X-Execution-Authorization`) bound to the app, the route's capability, the tenant (`AGENTIC_OS_TENANT` when the deployment
    is bound to one), the idempotency key and the exact request (body plus path parameters); single-use through the replay store. A request that carries one IS an agent
    request: no other header on it grants human privileges, so an agent cannot impersonate a human decision.
  * **service**: `X-Service-Token` mapped to explicit capabilities (`AGENTIC_OS_SERVICE_TOKENS`); never satisfies a human-only route.
  * Access classes: `read_only`, `public_intake` (needs a written rationale), `stages_only`, `authority_required`, `human_only`, `operator_boundary`.
  * In the production profile an unauthorised call is refused before the handler runs, and any write request that matches no declared policy is refused (fail closed).
    In other profiles the context is computed (so a marker is still earned only from an authenticated context) but nothing is blocked.
* The 45 routes, classified in `apps/<app>/route_policy.py` (verified by reading each handler): **27 `stages_only`** (the `/agent/run`, `/api/agent`, `/api/run`,
  `/api/discover`, `/retention`, `/api/chat` family: the core stages any consequential action unless handed an approval marker), **7 `authority_required`**
  (billing outcomes/refit/decide, social reel record/schedule, vibex generate/draft), **4 `human_only`** (privacy fulfil, billing decide-approve, edge-sentinel case
  respond and execute), **4 `read_only`**, **3 `public_intake`** (privacy request, intake, verify: the only anonymous routes, named in a CI test).
* **Approval markers reach the apps without rewriting their routes:** the middleware sets a request-scoped marker (a context variable, so it reaches sync handlers in the
  threadpool) and the existing marker helpers (`gate.py` in billing, books, compliance; `core.operator_approval` in crm, outreach, support, growth-assistant) fall back to it.
  The body can never supply it. An **agent** earns a marker only for actions listed in the route's `agent_actions`, which is empty everywhere. This is deliberate: a name-based
  deny-list would have been wrong (billing's `chase_overdue` action runs the mandatory `dunning` capability), so on multiplexed routes agents get previews only, and their
  governed path is the operator `/invoke` or the direct-action flow on a single-purpose route.
* **Direct-action flow for REST:** `RouteCatalog` lets `ActionAuthorizer` issue authorizations for route capabilities from the same envelope; path parameters are selectable
  resources (`path.<param>`); a REST side effect with no undo is tier 2, so the owner must raise a grant to `max_tier=2` explicitly; a `stages_only` route is authorized only
  for its `agent_actions`.
* **F21:** `approval_required` is now enforced on every operator `/invoke` in every profile. Without a verified human approval the handler does not run; it returns the
  staged result the runtime already treats as "not executed" (production refuses side-effecting ones with 403). `AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE=1` is an explicit
  testing override that production ignores. Caveat: the staged result is generic, so a handler's own richer preview is not shown on this path; the legacy handler-level tests
  that exercised direct execution now opt into the override through their conftest, and the default behaviour has its own tests.
* **CI gates:** `apps/test_route_authz_registry.py` imports every real app and requires an exact match between the write/WebSocket routes it serves and its policies (new
  route without a policy fails; stale policy fails); `tests/test_entrypoint_inventory.py` requires a reviewed entry for every non-REST entry type in the apps and pins the
  kernel's unauthenticated write routes so a new one fails and a fixed one must be removed.

**Reachability audit beyond the 45 routes** (`audit/operator_capability_inventory.md`, regenerated): no WebSocket commands, no webhook-shaped routes and no scheduler
libraries in the apps. Five non-REST entry points, each reviewed in `apps/other_entrypoints.json`: the billing outcome observer thread (off by default, read plus learning
only), its lifecycle hooks, the growth-assistant background content job (no approval marker, so the core stages consequential actions), and the suite server's mount.
Operator `/invoke` is covered by F19. Internal RPC: the kernel services have **44 write routes; 19 show no authentication marker** (heuristic, pinned in
`audit/kernel_unauthenticated_routes.json`).

**F22 (new, High): the kernel's own services.** 14 of those 19 are the Projects API (`projects_api.py`: capture, today approve/snooze/dismiss/batch/undo, connect app,
sidekick, source propose). Its "identity" is a demo session or the socket peer and it ships over a sample provider, i.e. it is a demo surface. **It must not face a real runtime
until it is authenticated and its write routes get policies (the same layer applies).** The rest: `control_plane` permissions preview, intelligence `quote`/`resolve`,
`mission/api` create, unified `sidekick`. Not changed here; pinned so none can be added.

**Not done / limits, stated plainly:**
* The layer enforces only in the production profile; elsewhere it is advisory.
* The 27 `stages_only` routes are reachable by anyone; their protection is the core's staging gate (pinned per app by that app's AST test), which this change does not re-verify.
* `read_only` and `public_intake` are classifications from reading handlers, not proofs.
* The kernel services (F22) are inventoried and pinned, not enforced.
* Network egress and raw outbound HTTP are a separate workstream.
* Verification correction: an earlier "all apps pass" for the operator-boundary PR was run from `apps/`, where the test venv resolved `agentic_os` to a stale installed copy rather than the repository. All app results for this change were run with the repository on `PYTHONPATH` (2,014 passed); that earlier check should be treated as not having exercised the repository code.

## Step 6: the network boundary (F10) - a separate workstream from F20/F21
Token checks cannot constrain raw HTTP or shell access from a workload (F10: one flat compose network, ~60 modules using `urllib` directly).
**Implemented:** `deploy/egress/proxy.py` (allowlist forward proxy, deny by default, SSRF guard that resolves once, checks every address and connects to the address
checked, JSON decision log), `forward.py` (inbound forwarder: a service stays on the internal network and one port is relayed to one fixed target), an
optional Compose overlay (`deploy/apps/compose.egress.yml`: the `apps` network becomes internal, the proxy is the only exit, the three cores' published ports go
through forwarders) and a Kubernetes NetworkPolicy set. The merged Compose file was checked with `docker compose config`: only Caddy and the three forwarders publish
ports and only they, the proxy and Caddy touch a non-internal network.
**Evidence:** 44 tests (40 proxy, 4 forwarder: policy grammar, 12 non-public address classes, mixed DNS answers, tunnels and plain HTTP over real sockets, limits, reload) fail when the
SSRF check, the apex rule, default-deny or the mixed-answer rule is removed (15, 1, 5 and 1 failures). `deploy/egress/selftest.sh` ran on a throwaway Docker network:
a client on an internal network could reach nothing directly (origin IP, the internet, the origin by name), reached an allowlisted origin only through the proxy, was
refused (403) for an off-list host and for the metadata address, and every decision was on the log. On Docker 29.9.1 an internal network also does not resolve external names.
**F10 status: mitigated for deployments that adopt the overlay or the policy; not closed by default.**
**Not done / limits:** opt-in; global allowlist (every service reaches every listed destination); no payload inspection; `private` rules (the default allows a model server on the
host) are deliberate holes; Caddy keeps outbound access; Kubernetes DNS is a tunnelling channel unless restricted; the Kubernetes manifest is YAML-validated only (the available
kubeconfig points at a deleted cluster, so it was not dry-run against an API server) and references an image that is not published or signed yet; the compose overlay was
validated statically and the mechanism live, but the full stack was not brought up with it (the Docker host was in use by another test run). Edge Sentinel does not consume the
decision log yet.
