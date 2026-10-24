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
| **F19 Operator `/invoke` and direct application handlers** | **NOT closed.** Any caller with a valid operator or tenant token runs the handler directly: no envelope check. Only 7 of 53 side-effecting app capabilities are wrapped in `gated()` (heuristic count of `side_effecting=True` in `apps/*/operator.py`); the mission runtime gates mission-originated calls, but a direct call is not a mission-originated call and a body `mission_id` is forgeable. **High.** Fix needs a runtime-issued, input-bound, single-use authority token (the approval-token pattern), so the operator can tell a mission-gated call from a direct one; direct callers then need an envelope. Not built: do not claim direct calls are governed. |
| Raw tool calls / raw outbound HTTP | **Not enforceable in-process.** No agent shell tool was found in the open core, but ~60 modules use `urllib` directly and the compose network is flat (F10). Needs the network boundary (step 6). |
