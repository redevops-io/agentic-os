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

## Step 7: hard limits in the serial loop, durable lease records, reconciliation (F12, F16, F17; the groundwork for the loop supervisor)
**Hard limits (F12).** The serial mission loop now enforces its ceilings instead of recording them: an absolute `Mission.deadline`, `Budget.wall_clock_s`, `Budget.usd` and
`Budget.tokens`. Deadline and wall-clock are checked before each wave; usd and tokens before dispatching a wave that would pass the cap (executed nodes plus the wave about to start).
A reached ceiling stops the mission with a `HardLimitReached` event and `outcome = {blocked: hard_limit, limit, used, cap}`; nothing further is dispatched. Extending the budget and
continuing is the owner's decision (or the supervisor's, next PR), not a silent retry. Semantics to know: `wall_clock_s` counts the ACTIVE run time of one driver segment, so time parked for
a human is not charged but a mission resumed across many segments is not summed (the supervisor enforces cumulative time at the loop level); tokens were never checked at plan time and
still are not; fork/join keeps its own checks. Default budgets (5 usd, 86,400 s) did not change any existing behaviour (1,128 mission tests, no regressions).
**Durable lease records.** A reservation now records its identity: kind (mission, gateway, fleet, direct), mission, node, attempt, operator, capability, tenant, side-effecting flag, risk tier,
envelope digest, idempotency key, `dispatched_at`, `execution_id` and, when a result carries one under a conventional key, the provider's reference. Memory and SQLite ledgers store it; an older SQLite
ledger is migrated in place. `AuthorityGuard.lease()`, `annotate()` and `outstanding(detail=True)` expose it.
**Reconciliation (`mission/reconcile.py`), as decided.** RESERVED with no dispatch: in production released automatically once older than the authorization lifetime (no authorization can exist for it);
outside production only when a probe says the effect did not happen. DISPATCHED/UNKNOWN: settled when an independent probe confirms the effect (provider reference recorded), released when a probe
authoritatively says it did not happen, otherwise kept counted, flagged `needs_review` and emitted as an `UNKNOWN_OUTCOME` event. No probe, a probe that raises, a non-authoritative "not found", or a
"not executed" that contradicts a recorded provider reference all stay unresolved. `resolve_manually()` and the CLI (`python -m agentic_os.mission.reconcile list|run|resolve`) let a named person settle or release
what no probe can, with their name and reason on the durable record.
**A restart can no longer turn uncertainty into permission to repeat.** If a node's reservation was already dispatched by an earlier attempt (a retry after a timeout, or a run resumed after a restart), the
runtime refuses to send it again, in every profile (`RedispatchRefused`; fork branches too). Before this, outside production the runtime would silently call the executor a second time. Tested for development
and production.
**Evidence:** 43 tests. Removing the hard limits fails 5; allowing re-dispatch fails 2; removing the contradiction guard fails 2; treating a failed probe as "not executed" fails 4; accepting a non-authoritative absence
fails 1. Root suite: same 13 failures as main.
**Not done, stated plainly:** (1) **No provider probes ship.** The reconciler and `idempotency_probe` exist, but nothing registers a probe for any real provider (Lago, Gmail, Postiz, Apollo...), so today every unknown
outcome goes to a human until someone writes a probe for that provider; whether a provider's "not found" is authoritative is a per-provider fact this code cannot know. (2) Nothing runs the reconciler on a schedule yet;
the loop supervisor will. (3) The mission runtime's own lease map is still in memory: after a restart a reservation is found by re-evaluating the node (same lease id), which is what makes the refusal above work, but a mission that is
never resumed leaves its reservation for the reconciler or a human. (4) Fork/join budget checks, and the operator-side settlement for direct authorizations, are unchanged.

## Step 8: unknown outcomes park the mission and the same mission resumes (`fix/mission-unknown-outcome-park-resume`; P0 for autonomous workflows with external side effects)
Why: #553 made a dispatched reservation un-sendable-twice (at-most-once), but ended the mission FAILED, which is safe and not recoverable. Measured before this change (crash after dispatch,
fresh runtime over the same log and ledger): no guard resumed and finished; guard, before #553, sent the effect twice; guard, #553, did not resend and the mission ended FAILED.
**What changed**
* New durable state `WAITING_RECONCILIATION`. A node whose dispatched reservation has an unknown outcome parks the mission instead of failing it. Two triggers: a re-dispatch of an already-dispatched
  reservation (a retry or a run resumed after a restart), and a side-effecting call that errored after it may have been sent (previously a failure plus compensation of earlier steps; now neither:
  nothing is declared failed or undone on a guess). An error that proves nothing was applied (`not_applied`) still fails normally.
* **Preserved across restarts:** the park is a `ReconciliationRequired` event (node, capability, reservation id, attempt, idempotency key, reason), so it is derived from the log, not from mutable state:
  `run()` is a no-op while it is unresolved (the log does not grow however often it is called, and a suspend/resume or a state overwrite cannot skip it); `rehydrate_in_flight` restores it; the node
  attempt is restored from the recorded dispatches; completed nodes stay completed; the inbox and the mission view list it.
* **Authenticated, evidence-backed reconciliation:** `POST /missions/{id}/reconcile` with `{node_id, outcome: executed|not_executed, evidence: {kind, reference, ...}, reason, result?, resume?}`. Tenant-scoped
  (404 cross-tenant); approver or admin credential only (a requester credential is never accepted, stricter than approving); the actor is the credential, never the body; evidence needs a recognised
  kind and a checkable reference (a bare assertion is rejected); a reason is required; with no credential configured the endpoint is disabled. Everything is recorded on the mission log and on the durable lease.
* **Resume of the SAME mission without rerunning completed work:** `not_executed` releases the reservation and the node re-gates under a fresh reservation and runs once; `executed` settles it and marks the
  node done from the evidence (a `NodeSucceeded` flagged `reconciled`, with who and why; no handler call). If a later step reads what that node produced, the provider's real result must be supplied (refused
  otherwise, before anything changes). `resume=false` leaves the mission PAUSED for the owner.
* **Concurrency:** validation happens before any change; the ledger's atomic transition decides (first resolver wins; a conflicting second one gets 409 and changes nothing); the whole operation holds the
  mission's run lock, so a concurrent run or a second reconciliation cannot dispatch twice; the same decision repeated converges without re-deciding; across replicas the ledger's atomic dispatch still allows
  exactly one send. In a concurrent wave, siblings' results that already came back are committed, never discarded.
**Two defects my own regression tests caught while building this** (both would have shipped): restoring `node.attempts` on rehydrate made a re-run's reservation id `...:a2`, a fresh lease, so the refusal silently
stopped firing (fix: re-gating a node reuses its unresolved reservation); and a refused re-dispatch still appended a `NodeDispatched`, so the attempt count drifted on the next restart (fix: take the reservation
before recording the dispatch).
**Evidence:** 39 tests including the three-way comparison with a negative control (a harness that provably sees a duplicate send), repeated restarts, both outcomes with a resume, evidence/actor/reason validation,
conflicts and idempotency, 8 rounds of concurrent reconcile and run racing a conflicting decision, a replica race on the ledger, suspend/resume bypass attempts, a concurrent wave, and the API credential matrix.
Seven mutations are each caught (terminal FAILED restored: 34 failures; duplicate send restored: 35; run loop ignoring the park: 1, found by asserting the log does not grow; run lock removed: 8; conflict check removed: 2;
fresh-lease bug: 35; attempts not restored: 2). Root suite: same 13 failures as main.
**Not done, stated plainly:** (1) no provider probes, periodic reconciliation, retries or notifications: those are the supervisor's; today a person reconciles. (2) Fork branches keep their own failure semantics
(`UNKNOWN_EFFECT`); only the serial and concurrent-wave paths park. (3) One driver per mission is still assumed across replicas (the run lock is in-process): the ledger prevents a duplicate send, but two replicas driving
one mission could both record events. (4) A reconciled `executed` node with no downstream reader is completed with a placeholder result flagged `reconciled`; a person asserting "it executed" is trusted to the extent of their
evidence, which is recorded.

## Step 9: the reconciliation supervisor, mission ownership with fencing, independent progress (first of two supervisor PRs)
Owner decision (2026-10-24): use bounded retries with backoff before escalating, but never retry the business action while its outcome is UNKNOWN; only the read-only probe repeats. A non-authoritative
"not found" means not observed, not confirmed not executed, and the supervisor, not each adapter, enforces that. Guide for adapter authors: `docs/MISSION_EXECUTION.md`.
**Implemented**
* **Evidence contracts (`mission/probes.py`).** A probe returns a structured `ProbeResult` (CONFIRMED_EXECUTED, CONFIRMED_NOT_EXECUTED, INCONCLUSIVE, TRANSIENT_ERROR, UNSUPPORTED) with evidence provenance, provider reference,
  observed time, the real result and Retry-After. Each adapter is registered with a `Qualification` (eventually consistent, documented visibility window, authoritative-negative). The registry judges every answer: a negative is accepted only
  from a qualified adapter, marked authoritative by the result, with evidence, and past the visibility window; a positive needs usable evidence provenance; everything else is downgraded. A generic HTTP 404 is INCONCLUSIVE (`from_http_status`).
* **Durable scheduler and the policy table (`mission/reconcile_jobs.py`).** Executed with evidence: settle, restore the result, resume. Authoritative negative: release, re-gate, retry the action once. Non-authoritative not-found and
  transient errors: retry the probe with exponential backoff, jitter and a bounded Retry-After. Unsupported, conflicting evidence, a positive that later steps need but that carries no result, and an exhausted budget: escalate; the
  mission stays waiting and **nothing is resent**. Probe attempts, the next eligible time, the last result, the escalation and its notification state live in SQLite: a restart does not reset a budget (5 probes total across processes,
  not 5 per process), replicas claim a job with a compare-and-swap and a stale claimant is fenced out by a claim token, an expired claim returns a crashed worker's job, and the escalation transition happens exactly once. The first probe waits
  for the adapter's documented visibility horizon.
* **Mission ownership with fencing (`mission/ownership.py`).** A durable lease per mission with a monotonically increasing fencing token (a takeover always advances it, a release never moves it back); `FencedEventStore` checks the token INSIDE the append, in the
  same critical section under SQLite's write lock, so a stale driver cannot interleave with the new owner (a test holds an in-flight write and shows the takeover blocks behind it). A heartbeat renews a live owner; a lost lease stops writes. The supervisor takes the
  mission lease around a resume and, if another replica owns the mission, skips it without spending an attempt.
* **Independent progress.** A node awaiting reconciliation blocks only itself and (transitively) its dependents; independent ready nodes keep running, including in later waves, and a flat plan completes everything that does not depend on the unknown node.
  When nothing independent is left the mission waits and writes nothing. Dependents stay blocked until reconciliation. In a concurrent wave the siblings' results are committed.
* **Wait-state classification (`mission/waitstate.py`).** `WAITING_RECONCILIATION` (scheduled observation or escalated), a human approval, paused, active and genuinely hung are distinguished; only a RUNNING mission with nothing recorded for too long is
  `watchdog_eligible`, so a healthy waiting mission is never sent into automatic replanning.
**Evidence.** 40 new tests plus the 39 from #554 (79 together). Twelve deliberate breakages are each caught: accepting a negative from an unqualified adapter (found only after I added a case isolating it from the consistency rule), ignoring the visibility window,
resending when the budget is exhausted (8 failures), dropping the contradiction check, not checking the claim token, resetting a job on rescan (21), re-claiming sent notifications, not checking the fence at append, a check that is not in the writer's critical section,
not blocking dependents (found only after I added a multi-wave case: it showed a duplicate refusal written every wave), non-transitive blocking, and calling a waiting mission hung. Root suite: the same 13 failures as main; apps: 2,014 passed.
**Not done, stated plainly**
* **No provider adapters ship.** The contract, enforcement and scheduler are in place, but nothing registers a probe for Lago, Gmail, Postiz, Apollo etc. With no adapter a job escalates as "no reliable lookup" and a person reconciles; automatic resolution begins when an adapter is written and
  qualified for that provider. Qualifying an adapter (is its lookup strongly consistent by our idempotency key?) is a per-provider fact this code cannot know.
* **Fencing has a stated limit.** It serialises writers that share the lease database (replicas on one host or a shared filesystem). A mission log that cannot take part in that critical section needs the log itself to reject stale writers (e.g. a conditional append on an owner epoch). The runtime
  is not yet wired to drive every mission under a lease; only the supervisor's resume is. Driving all missions under a fenced lease belongs with the goal-loop controller. Lease expiry uses wall clocks: replicas with badly skewed clocks weaken it.
* **Fork parity was not done in this step** (closed in Step 10): fork branches still used their own failure semantics (`UNKNOWN_EFFECT`); only serial and concurrent-wave nodes followed this contract.
* **Notification delivery is at-least-once with a stable key**, not exactly-once: the escalation is recorded exactly once, a failing notifier is retried a bounded number of times, and a crash between a successful send and recording it can send once more (receivers dedupe on the key).
* Backoff values are starting values, not provider guarantees. The goal-loop controller (renewable execution leases, soft versus hard limit ladder, diagnose-then-replan, verified completion, cancellation propagation) is the next PR.

## Step 10: fork reconciliation parity (`feat/fork-reconciliation-parity`)
Owner direction (2026-10-24): bring fork outcomes into the same reconciliation contract before claiming autonomous-swarm recoverability, and prove it with a crash-and-restart test of three branches (one verified successful, one UNKNOWN, one still running): after the restart the
successful branch does not rerun, the UNKNOWN branch is not resent, and the third finishes. Guide: `docs/MISSION_EXECUTION.md` (section "Fork branches").
**What was different before.** (1) A side-effecting branch whose call failed ambiguously became a terminal `BranchFailed`, which could fail the join, run the failure policy and report `UNKNOWN_EFFECT`: safe against resending, but not recoverable. (2) A branch that an older driver had in flight
when the process died was re-dispatched on restart with the gate skipped (`resumed`) and with no look at its reservation: in a new process the in-memory lease map is empty, so nothing stopped a second send of a call that was already dispatched.
**Implemented**
* **One record, one view.** An unknown branch writes the same `ReconciliationRequired` a plan node writes (plus `fork_id`/`branch_id`), so `pending_reconciliation`, the inbox, `missions()`, the supervisor's probe jobs and the wait-state classification see it without a fork special case. `fold_fork` derives a new
  branch state `reconciling` from required-minus-resolved (event-sourced, so it survives restarts); it counts as "remaining" in the join, is never scheduled, and is neither failed nor retryable.
* **An ambiguous failure parks the branch.** The reservation stays counted (UNKNOWN), the branch waits, nothing is resent. A failure that says `not_applied`, and any failure of a read-only call, are unchanged (retry / fail). Without an authority ledger the old behaviour is kept (there is no reservation to reconcile).
* **The fork waits instead of closing around an unknown.** When everything that can run has run and a branch awaits reconciliation, the mission parks in `WAITING_RECONCILIATION` and `run_fork` returns `awaiting_reconciliation`. If the join can no longer succeed it does not close and compensate either: what to roll back depends on a call that may have applied
  its effect. Cancellation and deadlines still close it; a cancelled branch keeps its open reconciliation and is reported `UNKNOWN_EFFECT`.
* **Resume reads the reservation** (`<mission>:<node>:a<attempt>`, derived, so it needs no memory of the old process): reserved = never sent, send it; dispatched/unknown + side-effecting = park, never resend; dispatched/unknown + read-only = return the reservation, re-gate and re-run under the same key (this is how the "still running" branch finishes);
  settled without a recorded result = park for the result; released = clean retry (or a clean failure if no attempts are left). The reservation's single dispatch remains as a backstop, and a branch recorded as succeeded whose reservation is still open (crash between the success and the ledger) is settled from the log.
* **Resolution goes through `reconcile_node`** (same validation, ledger first-wins, conflict on a contrary second decision, idempotent repeat). Executed: the branch is completed from the evidence (a branch that `produces` a fact needs the provider's real result). Authoritative negative: a new attempt under a fresh reservation, or a clean failure if its attempts are
  used up (not reported as an unknown effect). The same fork then continues from its log: succeeded branches are not rerun. After a restart the owner re-supplies the spec (`register_spec` + `run_fork`; inputs are not in the log); until then the mission is PAUSED, not silently waiting.
* A bug found while testing: an idempotent repeat of a branch resolution fell into the plan-node path and re-ran the mission's plan; branch resolutions now route first.
**Evidence.** `tests/test_fork_reconciliation_parity.py`, 23 tests, run in the production profile with a durable SQLite ledger and log (a restart is a new runtime over the same files; the provider executes each key once and counts every send). The headline test: A succeeded, B UNKNOWN, C running when the process dies; after the restart A and B are sent once in total, C is
re-sent under its key and finishes, the fork is `awaiting_reconciliation`, and no BranchFailed/Join event exists; further restarts while unknown never send B; evidence executed completes the same fork; an authoritative negative retries as attempt 2. Twelve deliberate breakages are each caught (ambiguous error fails the branch; in-flight side-effecting branch resent; read-only branch parked
instead of re-run; reserved lease not re-registered; success never settled; failed join closes around the unknown; negative never retries; `reconciling` not a distinct state; fork branch treated as independent work in the wait-state; backstop removed; settled and released leases resent). The existing fork, authority and compensation suites (166 tests) pass unchanged; the serialised form of a fork without an unknown branch is byte-identical (the golden fixture).
**Not done, stated plainly**
* **The supervisor's automatic resolution is exercised with a test probe only**; no provider adapter ships (unchanged from Step 9). With no adapter the job escalates and a person reconciles.
* **A mission can hold both a plan and a fork.** A branch awaiting reconciliation does not block the plan's own independent nodes (a pending branch is never "independent work" of itself, but the plan graph does not depend on it). A mission whose fork is satisfied while a cancelled branch is still unresolved will not be reported succeeded until that reconciliation is resolved.
* **Without an authority guard there is no reservation, so no fork reconciliation**: the legacy failure semantics remain for that configuration (the production profile requires a guard).
* A fork closed by deadline/cancel with a branch in flight still cannot kill the call (late results are kept as receipts); that is F16, not changed here.

## Step 11: the goal-loop controller (`feat/goal-loop-controller`)
Owner direction (2026-10-24): after fork parity, the controller builds on one recoverable execution model. Guide: `docs/MISSION_EXECUTION.md`, section "Goal loop".
**Implemented (`mission/goal_loop.py`, with its own durable log; each iteration is an ordinary mission)**
* **Hard limits are ceilings** (iterations, active time, usd, tokens, deadline): reaching one stops the loop (`limit`) with the reason on the record; only a named owner with a reason can raise them (`extend_limits`; a ceiling only goes up). A loop cannot be started without a ceiling.
  Active time is durable (a restart cannot reset it). `BUDGET_NEAR_LIMIT` is raised once per cap at 80%.
* **Soft thresholds never terminate**: the same failure repeating, no new completed step for N iterations, or the same plan again with nothing new each produce a recorded diagnosis, then a bounded number of driver replans, then an escalation (`escalated`, resumable: `resolve_escalation` continue/stop, named actor and reason).
  Progress is counted as new completed step signatures, so a failing iteration that completed something new is not a stall.
* **Verified completion**: only an independent verifier with usable evidence completes a goal. A succeeded mission without one ends `completed_unverified`; a verifier that raises, returns no evidence or says no leaves the goal open.
* **Uncertainty and authority are never planned around**: while any iteration's mission has an unresolved reconciliation the loop starts nothing, diagnoses nothing and replans nothing (a new plan could resend the unknown effect); an authority denial, a mission-level ceiling, an unknown effect and a failed compensation escalate immediately; an envelope that needs extending parks for a person and the loop waits.
* **Ownership enforced at the write**: the loop log is fenced (a stale controller's append is refused), each iteration's mission runs under its own fenced lease, and the production profile refuses a loop without leases or without a `FencedEventStore` runtime. Owner decisions (cancel, resolve, extend) are append-only statements that do not need the controller's lease.
* **Checkpoint/resume**: state is a pure fold; a restart resumes the open iteration (never a second one for the same number), reruns nothing finished, and keeps every counter.
* **Cancellation**: the intent is recorded first, then the live mission is cancelled (the serial runtime now also stops dispatching after a cancel, between waves and between nodes of a wave); the cancellation record lists unresolved reconciliations rather than implying a clean stop.
* **Wait-state classification**: `classify_loop`; new `Wait.STALLED` (the controller's own case, not a watchdog case); only a quiet RUNNING loop is `watchdog_eligible`. `tick()` continues loops whose mission a probe or a person resolved. Events for a sentinel: LOOP_STALLED, BUDGET_NEAR_LIMIT, LOOP_ESCALATED, LOOP_LIMIT_REACHED, LOOP_COMPLETED, LOOP_CANCELLED.
**Evidence.** `tests/test_goal_loop.py`, 41 tests on real runtimes (production profile and a durable authority ledger where it matters). Twenty-three deliberate breakages are each caught (three survived at first: the usd boundary, the same-wave cancel and the actor check on resolve; each got an isolating test).
**Not done, stated plainly**
* **No shipped driver or verifier.** The controller takes a `LoopDriver` (creates the iteration's mission, proposes a replan) and a `Verifier`; the LLM-backed replanner and per-goal verifiers are a deployment concern. The default is "no replan available", which escalates.
* **Missions outside the controller are not forced under leases.** A direct `rt.run`, the HTTP API or the supervisor of a runtime whose store is not fenced still write unfenced; fencing covers loops (and the supervisor's resumes). A managed (leased) mission can only be written by its lease holder, so a console that reconciles one must take the lease first.
* **Cross-process cancel is boundary-level.** In the same process cancel stops dispatch immediately; from another process the intent is recorded and the owner acts at its next boundary. A call already in flight cannot be killed (F16).
* **Active time loses the segment in flight at a crash** (it is recorded when a run segment ends); `max_iterations` still bounds a crash loop. A crash between creating a mission and recording the iteration leaves an unrun orphan mission (no effects).
* **Child-agent identities and scoped credentials, context provenance and Edge Sentinel consumption** are the next items in the agreed order.

## Step 12: child-agent identities and scoped credentials (`feat/child-agent-identities`; F5, F13, F14)
Owner order: after the goal-loop controller, child-agent identities and credentials. Guide: `docs/SECURITY_MODEL.md`, section "Child agents".
**What was missing.** `AuthorityEnvelope.attenuate` could derive a provably smaller envelope but nothing stored one, enforced it or tied it to a caller. A delegated worker ran as its parent: same identity in every audit record, no share of its own in the budget, no way to switch it off without switching off the parent, and the gateway took the envelope key from a name the caller supplied.
**Implemented (`mission/children.py`, `mission/authority.py`, `agent_gateway`)**
* **Spawning is a sequence of refusals, each undoing the last**: the parent must be allowed to delegate (not revoked or expired, `may_delegate`, a unit of `max_children`, a valid chain of its own); the envelope is derived by `attenuate` (never a grant, limit, lifetime or depth the parent lacks);
  the child's allowance (usd, tokens, actions) is CARVED OUT of the parent's counters as a reservation, so the parent cannot spend what it handed out and the sum over a tree is bounded by its root; a durable registry atomically caps active children per parent (`max_parallel`) and per tree; then the envelope is stored and a credential issued.
  A refusal leaves no residue (no quota, no allowance, no child). When a child ends, what it did not use returns to the parent; anything it left dispatched or unknown is listed, never dropped.
* **The lineage is enforced at every decision** (`AuthorityGuard.chain_denial`, used by `authorize` and delegation): every ancestor must exist, be the same tenant, unrevoked, unexpired and current in its revocation epoch, and still COVER what the child holds. A child's authority is the intersection down its chain, so revoking, expiring or narrowing any ancestor stops every descendant at once without a list to keep in sync;
  a child whose parent is not on record is denied. `AuthorityEnvelope.parent_mission_id` is new and omitted when empty, so every existing envelope serialises and digests exactly as before (the one existing test that stored a child without its parent now stores the parent too).
* **A signed, short-lived, revocable credential** (domain-separated key; bound to tenant, identity, lineage, envelope digest and optionally one client). It is honoured only while the registry says the child is active with this very token id, the stored envelope still has the digest it names and the chain is valid. Close, revoke, rotation and an altered envelope each kill it at once.
* **The gateway takes identity from the credential**: with `child_credentials` configured the envelope key comes only from a verified credential (a caller-supplied mapping cannot be ridden), a caller claiming an `agent:` identity without one is refused, every failure gets the same answer (no oracle), and the reservation record carries `agent_id`, principal, parent and depth.
* **Ending children**: `close`, `revoke` (named actor and reason), `close_tree`; a cascade closes descendants first; the first decision stands; `tree()` and an append-only audit log give the lineage and usage; events CHILD_SPAWNED / CHILD_REFUSED / CHILD_CLOSED / CHILD_REVOKED for a sentinel.
**Evidence.** `tests/test_child_agents.py`, 53 tests (durable SQLite guard and registry, a restart over the same files, the gateway end to end). Twenty-eight deliberate breakages are each caught (four survived at first: ancestor expiry, the chain check at delegation, the registry status check and the `agent:` identity rule; each got an isolating test).
**Not done, stated plainly**
* **The credential is a bearer token.** It is short-lived (default 15 minutes), bindable to a client, revocable instantly and never wider than the child's envelope, but anyone holding it before expiry can present it from the bound client. Per-call proof of possession is not built.
* **Broker/secret scoping is unchanged**: a child's credential is an authority credential. Issuing per-child secrets from Vault/OpenBao (the executor's broker seam) with the child's chain as `authority_context` is not wired.
* **The mission runtime does not resolve a child's missions automatically**: a runtime whose `authority_key` should map a child's mission to the child's envelope uses `ChildRegistry.key_for_principal` (provided, not wired by default).
* **A crash between spawn steps can leak** one unit of child quota or one allowance reservation (both stay counted); it cannot create authority. Reclaiming orphans is not automated.
* Context provenance and compaction integrity, Edge Sentinel consumption, F22 and the demo environment remain.

**Step 12 follow-up (`fix/child-credential-lives-with-mission`).** The first version gave the credential a fixed 15-minute life, which would have cut a longer mission off mid-run and left no way for the child to continue. Owner direction: the short-lived credential must stay alive while the mission runs, not be revoked and re-issued in the middle of it.
Now: one token for the whole mission; it expires only with the child's envelope. Its short life is a liveness lease in the registry that the OWNER extends (`keepalive` / `heartbeat`), never the token's own use; when the driver stops the token dies within one lease (60 s default), and a lapse is recoverable by the same token while close, revoke, a revoked/narrowed ancestor and envelope expiry are final. `reap_lapsed` closes abandoned children and returns their allowance. 12 more tests (63 in the file); 12 more deliberate breakages caught (one, the registry-level status guard, survived until it got its own test).
Still true and stated plainly: the token is a bearer token while its lease is live; a driver that dies and stops heartbeating strands its child until `reap_lapsed` runs (the allowance stays counted until then); the goal-loop controller does not yet run `keepalive` for children automatically.

## Step 13: context provenance and compaction integrity (F11), and Edge Sentinel consumption (`feat/context-integrity-and-sentinel-feed`)
Owner order: the last item of the agreed sequence. Guide: `docs/SECURITY_MODEL.md`, section "Context integrity and the sentinel feed".
**What was open.** F11 was NOT_INSPECTED: `compaction.py` replaces early history with a model summary written as a SYSTEM message. That loses the owner's scope (goal, acceptance, constraints, forbidden actions live in the early messages) and promotes whatever tool, web or email content was in the summarized history into the system role.
And every component of the autonomy stack reported in its own shape; Edge Sentinel consumed none of them (Step 6 said so about the egress decision log).
**Implemented**
* **`mission/context_integrity.py`.** `ScopePin` renders the mission's authority (from its envelope) as one canonical block with a digest and pins it as the first message: never summarized, never shown to the summarizer, byte-identical after every compaction. `verify_scope` detects a missing, edited, demoted or non-first pin and any look-alike claiming to be pinned;
  `compact_with_integrity` repairs the pin (and reports `SCOPE_REINJECTED` / `SCOPE_TAMPERED`) instead of carrying on without it. Messages carry provenance (`prov.trust`); only `owner` and `system` are trusted and an unlabelled message is untrusted. Untrusted items are fenced for the summarizer (data, not instructions); a summary containing any untrusted item is `derived_untrusted`, fenced and NOT given the system role.
  Compaction fails closed: a summarizer that raises, returns nothing, or returns something not smaller leaves the history untouched. Every compaction leaves a record: what was dropped by trust class, source and digest (not the content), the summary's digest and trust, whether the pin was repaired.
* **`mission/sentinel.py`.** One canonical `SentinelEvent` and a durable, append-only `SentinelFeed` with adapters for the goal-loop controller, child agents, the reconciliation supervisor, context integrity, the egress proxy's decision log (refusals only; metadata/internal addresses rated higher) and the assurance sensors. Events have stable dedupe keys (a retried callback or a replayed log does not alert twice; a condition still true after `repeat_after_s` is reported again), sinks never raise into the component they observe,
  and the monitor `pull`s with a cursor it stores, so a restart loses and repeats nothing.
* **`apps/edge-sentinel/agent_events.py`.** The consuming end: each event at or above a floor (default medium) becomes the same evidence -> observation -> finding -> case spine a CrowdSec alert uses (case type AI_SECURITY, raw event kept verbatim as content-addressed evidence, idempotent). An event is a signal: the case is INVESTIGATING, the finding recommends no action, nothing is blocked, revoked or retried, and nothing is closed. `pull_cases` resumes from a feed cursor and can rebuild a case store from the feed alone.
**Evidence.** 46 new tests: `tests/test_context_integrity.py` (24), `tests/test_sentinel_feed.py` (15, fed by the real goal loop, child spawner and compaction), `apps/edge-sentinel/test_agent_events.py` (7). Twenty-nine deliberate breakages are each caught.
**Not done, stated plainly**
* **This does not detect prompt injection.** It makes injected text unable to claim the owner's or the system's voice and keeps the owner's scope from being summarized away; it cannot stop a model being persuaded by data it is allowed to read. Faithfulness of a summary to what it replaced is not verified, only that it cannot carry authority or replace the pin.
* **Nothing calls `compact_with_integrity` yet.** The existing `compaction.compact` is unchanged and still used by Sidekick and chat sessions; adopting the integrity wrapper in those callers is a follow-up (they must tag provenance on tool results for the trust classes to mean anything).
* **The components are not wired to a feed by default.** A deployment passes the feed's sinks as `on_event` and runs a poller that calls `pull_cases`; no service does so out of the box. Events carry the tenant only where the source knows it (loop events do not).
* **The egress adapter reads the proxy's log lines; it does not tail a file or a container's stdout.**
* F22 (the Projects API's unauthenticated demo surface), provider probe adapters (Lago, Gmail, Postiz, Apollo) and the demo environment remain.
## Step 14: the Projects API's write routes under the route-authorization layer (F22, the demo surface)
Finding F22: the Projects API (`projects_api.py`) ships over a sample provider and its "identity" is a demo session or the socket peer; 14 of the 19 unauthenticated kernel write routes were its. The audit said it must not face a real runtime until its write routes are authenticated and have policies.
**Implemented.** All 19 write routes of the Projects API have a policy (`agentic_os/projects_route_policy.py`) and `create_app` installs the same enforcement layer the apps use. In the production profile a write route with no policy, or without an authenticated context, is refused BEFORE any handler runs (a test asserts no provider method is reached); outside production nothing is blocked and the demo keeps working.
Decisions that belong to a person are `human_only`, so an agent's execution authorization or a service token can never make them: approve, reconcile, batch approvals, connect an app (and its start), confirm sources, policy promotion, and the demo's simulated outcome. Other state changes need an authenticated human with `projects:write`, an agent's authorization, or a service capability; `sources/propose` is a pure query (`read_only`).
**A bug fixed on the way.** The layer matched the policy against the full request path, so an app MOUNTED under a base path (`/projects`, how the hosted demo is served) looked like a route nobody classified and was refused in production even for the operator (and skipped off production). It now matches against the path left after the mount prefix, still failing closed for an unclassified path.
**Pin shrunk.** The scanner treats a module that installs the layer as guarded (the CI test that every route has a policy keeps that true), so `audit/kernel_unauthenticated_routes.json` drops from 19 routes to 5 and the generated inventory is regenerated.
**Evidence.** `tests/test_projects_route_authz.py` (31 tests, every route parametrized) and one regression test in `tests/test_route_authz.py` for the mount; 8 deliberate breakages are each caught (layer not installed, approve / batch / confirm / simulate-outcome opened up, a route left without a policy, the mount not stripped, the scanner trusting every file).
**Not done, stated plainly**
* **Reads are not covered**: the layer protects writes; the Projects API's GET routes still serve sample data without authentication (they carry no side effects, and the demo identity remains the socket peer or a session).
* **A production deployment of the Projects API now needs what the apps need**: an operator credential or tenant tokens, and a durable replay database (`AGENTIC_OS_REPLAY_DB`) or it refuses to start. The public hosted demo runs in the development profile and is unchanged.
* **The five remaining unmarked kernel routes** (`control_plane` permissions preview, intelligence `quote` / `resolve`, `mission/api` create, unified `sidekick`) are still pinned, not enforced; some are gated at router level, which the scanner cannot see.
* **Not built, and why:** provider reconciliation probe adapters (Lago, Gmail, Postiz, Apollo) need a per-provider fact this code cannot know (is the lookup strongly consistent by our idempotency key?), so each needs the provider's documentation or a live test account; the redevops.app demo environment and any infrastructure apply need your go-ahead.

## Step 15: an agent in the loop found a gap the scripted suites could not (direct gateway calls with an unknown outcome)
**How it was found.** The governed-properties suite (benchmark repo) drove the kernel with scripted code and no LLM. An investigation of how agents really reach it showed that the suites imported neither `agent-harness` nor `context-runtime`: the benchmark's workers run through the `sidekick` CLI (its own loop and context clipping), the apps' conversational agents use context-runtime's `AgentConsole` (single-turn, tools through the agent-harness registry), and a multi-turn agent is `agent_harness.loop.run_loop` with a context manager (context-runtime's archive-based `TranscriptCompactor`). None of them touches the mission runtime's reconciliation, and
`agentic_os.mission.compaction` (which Step 13 hardened) is called by none of them. A new scenario, `agent_in_the_loop`, puts the real loop and context manager in front of the governed gateway with a worst-case obedient model (it follows instructions planted in tool results, retries at once after a timeout, copies a context stub into an argument).
**The gap.** The mission runtime parks a node whose outcome is unknown and never resends it. A DIRECT gateway call did not: a side-effecting call that errored after it may have been sent left its reservation UNKNOWN, but the next request (same idempotency key or not) was a fresh call. The success-only idempotency replay does not cover a call that errored. With the model retrying after a timeout the effect happened twice (8 of 40 seeds, every seed with deterministic keys and a timeout).
**Implemented (`agent_gateway/gateway.py`).** A side-effecting DIRECT call whose earlier attempt (same authority key, capability and idempotency key) is still DISPATCHED or UNKNOWN is refused (`authority:outcome_unknown`) and reported to the `on_authority` observer; `unresolved_calls(key)` lists them; `reconcile_call(...)` resolves one with evidence, a named person and a reason (first decision wins; executed with a `result` makes a retry with the same key return the recorded result without a send; not executed returns the reservation so the action may be tried again).
In the production profile a side-effecting DIRECT call without an idempotency key is refused (a retry cannot be told from a new action). Reads and non-production profiles are unchanged. 9 tests; 10 deliberate breakages each caught (one, the capability in the match, survived until it got its own test).
**What stays true, stated plainly.**
* **A model that changes the idempotency key defeats the link.** The key is what ties a retry to an attempt; an agent (or a careless tool wrapper) that mints a fresh key per call looks like a new action. The damage is bounded by the child's grant (`max_calls`, per-action amount) and allowance, which the suite checks, not prevented. Tool wrappers should derive the key from the call (tool and arguments); the suite's even seeds do and show exactly one effect.
* **Gateway reservations are not on the supervisor's schedule.** `ReconciliationSupervisor` scans missions' `ReconciliationRequired` records; a gateway call with an unknown outcome waits for `reconcile_call` by a person. Probing it automatically is not built.
* **The agent-harness approval gate and agentic-os authority are separate systems.** An app's conversational agent stages a side-effecting tool for approval through the harness; the mission authority envelope, reservations and ceilings apply only where a tool is bound to the gateway or the mission runtime. Nothing forces that binding.
* **The gateway now feeds the sentinel**: `SentinelFeed.gateway_sink` (for `AgentGateway(on_authority=...)`) turns a refused retry after an unknown outcome, an out-of-scope attempt (what a planted instruction looks like from outside) and a call under a revoked authority into events Edge Sentinel opens cases for; allowed calls and other refusals are not events.
* **Context management differs by agent.** context-runtime's compactor archives (never summarizes), keeps the system prompt and task, and pins; Sidekick clips; nothing uses the summarizing `compact_with_integrity`. It remains a utility for a future summarizing caller, not a protection any current agent has.
* **Provider probes need `effect_ref`; Lago is the first adapter.** The supervisor could only ask a probe about a lease, which carries no provider identity. Reservations now record `effect_ref` (declared fields only, secrets skipped) from the runtime, fork-join and gateway paths. `LagoProbes` confirms subscriptions by `external_id`, cancels by a `terminated` record, refunds only as a candidate: a credit note on the invoice for the amount since dispatch is reported INCONCLUSIVE with the candidate named, because an unrelated refund with the same invoice and amount is indistinguishable from ours (`accept_unique_refund_match=True` opts in to confirming exactly one match). Limits: Lago refund POSTs have no idempotency identity; no adapter proves absence; `billing.dunning` is unsupported. Gmail, Postiz and Apollo adapters are not built.

## Step 16: SOC 2 readiness foundation in Edge Sentinel (`feat/edge-sentinel-soc2-foundation`)
Owner direction (cross-repository security and SOC 2 plan, 2026-10-24): Edge Sentinel, not the benchmark repository or `runtime-contracts`, owns the SOC 2 control registry, criteria references, evidence integrity, assessments, exceptions, review and restricted exports; other repositories are evidence producers; the benchmark measures Edge Sentinel's assessment accuracy independently.
Guide and inventory (ES-1): `apps/edge-sentinel/SOC2_READINESS.md`. Edge Sentinel is `agentic-os/apps/edge-sentinel` (no standalone repository was found).
**Implemented (ES-2, ES-3 foundation; `apps/edge-sentinel/soc2/`).** A durable, append-only, signed, hash-chained, tenant-scoped evidence vault; an 11-control catalog with AICPA criterion references; an assessment engine in which missing, partial, unverifiable, claimed-only or wrong-target evidence can never be a PASS; a ledger with superseding assessments, separation of duties and expiring exceptions; restricted and benchmark exports that cannot carry a certification claim; collectors that sign envelopes from sentinel events and reconciliation outcomes.
**Evidence.** 83 tests (vault 20, assessments 45, ledger and exports 18), including every seeded failure in the plan (missing MFA, stale privileged access, unreviewed change, backup without restore, missing log window, tampered artifact, cross-tenant access, a fabricated compliance claim, an inconclusive scanner target, an expired exception and attestation, duplicated and late evidence). 45 deliberate breakages are caught; the one survivor (the explicit replay check) is an equivalent mutant because the nonce's UNIQUE constraint produces the same result.
**Not done, stated plainly** (full list in `SOC2_READINESS.md`): no collectors for identity, CI, cloud or ticketing; no real period of evidence, so no operating-effectiveness claim; the vault is tamper-evident, not WORM; manual obligations stay manual; no routes. ES-4 (the synthetic assessment-accuracy benchmark with an independent oracle), ES-5 (operational evidence pipeline on a real scoped deployment), the CyberGym-E2E lane and the redevops-connectors reconciliation adapters (Lago, Gmail, Postiz, Apollo) are separate workstreams that remain.
* **Gmail, Postiz and Apollo probes (provider facts checked against their documentation on 2026-10-24).** Gmail documents no idempotency, no guarantee that a client `Message-ID` survives, and no search-index delay; `redevops-connectors` builds the message without a Message-ID, so today a sent message is only a candidate (same recipient, same subject, since the call): inconclusive by default. Setting a Message-ID in the connector would make the probe exact (it verifies the stored header). Postiz documents no idempotency, a 30-requests-per-hour limit on the list endpoint (one request per probe; 429 honoured) and a post `state` (QUEUE, PUBLISHED, ERROR, DRAFT) that is read honestly: ERROR is never success; its stored-content format is unverified against a live instance. Apollo's `add_contact_ids` skips contacts already in the sequence, so enrollment is state: a membership that began since the call is confirmed, one from before it is inconclusive. Findings: the connector's Apollo `contact.upsert` creates without `run_dedupe`, so a retry can create a duplicate contact; `outreach.sequence.configure` adds a step on every call. Neither can be probed. No adapter can prove absence.
* **SOC 2 assessment accuracy (first measurement).** A benchmark lane (multiagent-orchestration-benchmark `soc2_accuracy/`, synthetic fixtures with labelled truth, an oracle that does not run Edge Sentinel's procedures, no aggregate score) found one false PASS: SEC-CFG-01 ignored a producer's explicit `population_complete: false`. Fixed here. Not covered by this fix, and not measured as a gate: a scanner result that omits `population_complete` is still treated as complete for the scans it carries, so the completeness of the HOST SET in scope is whatever the collector sends, unsigned by any inventory; a host that was never scanned does not appear.
* **Evidence pipeline for Edge Sentinel (ES-5).** Collection is now code: a failed or refused read is a gap, never an empty payload that claims completeness, and windows already covered are skipped so a rerun cannot double-count. Design choices that came from the documentation, not convenience: GitHub's 2FA filters are owner-only (a 403 is a gap, not "nobody lacks MFA"); review `state` is not enumerated, so only the exact `APPROVED` counts and a user's latest review decides; a 404 on required status checks may mean "no protection" or "no access", so it is a gap and never "no required checks" (which the assessor would score as an unchecked release); an account list is emitted as `mfa_status` so it cannot satisfy the access-review or leaver controls. Not built: collectors for access reviews and leavers (an identity provider), host scans, backups and restore tests; deployments; rulesets. Untested against a live GitHub organisation.

