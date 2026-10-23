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
