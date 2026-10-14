# Mission fork/join

Status: implemented in `agentic_os/mission/fork_join.py` (Python spec) with event-folding parity in
`go/mission/fork.go`. Owner decision (binding): **forks are parallel branches within an existing Mission, not
independently authorized agents.**

## 1. Principles

* The parent Mission owns authority, budget, deadline and cancellation. A branch has none of its own.
* A branch is one capability invocation. It receives only the capability it declares (plus an explicit,
  mission-subset `requires` list); it can never widen the parent's grants.
* No branch approves anything. A branch that needs a human decision is parked through the same
  `NodeParked` -> `MissionRuntime.approve()` path as any node.
* Everything durable is an event. The fork state is a pure fold of the log; the join is evaluated only over
  persisted outcomes.
* A failed branch never invalidates a successful sibling's recorded work, and successful branches are not
  compensated automatically. The Mission's declared failure policy decides (default `HALT_AND_REPORT`).

## 2. Existing machinery that is extended (not duplicated)

| Existing | Used by forks |
|---|---|
| `concurrency.py` resource/conflict keys (`conflict`, `acquire`, `key_limit`) | branch scheduling holds back a branch whose keys are at limit (branch `Node` carries the capability's `concurrency_mode/key/resource_keys/max_parallelism`) |
| `SchedulePolicy.per_operator_limit`, `MissionRuntime.policy` | honoured per operator across a fork |
| `max_concurrency` pool | fork concurrency = `min(fork.max_concurrent, tenant max_concurrent_branches, runtime pool if >1)` |
| `WaveScheduled` / `SchedulerConfigured` telemetry | `ForkOpened` records the same effective-ceiling/`bound_by` facts |
| `_pre_exec_ok` admission seam, `_meter`, `_executor_for`, `context.resolve(CHECK_POLICY)`, verifier | called per branch exactly as for a node (so the enterprise overlay's authz/tenancy/metering gates apply unchanged) |
| `NodeParked` / `NodeResumed` / `NodeSkipped` / `ApprovalGranted` / `ApprovalRejected` / `ApprovalInvalidated` | reused verbatim for branch approvals, so `pending_human`, needs-you, gate_notify and the cockpit show a parked branch with no new code |
| `Executor.run` idempotency key + `InMemoryOperatorClient` dedupe | branch key derived from `(mission, fork, branch, attempt)` |
| `EventStore.subscribe` | cancellation (`MissionCancelled`) and policy tightening (`PolicyChanged`) are consumed by subscription: **no edit to `cancel_mission` / `change_policy`** |

The only edits to `runtime.py` are: a lazy `forks` property and a 4-line hook at the top of `approve()` that
hands branch node ids to the fork controller. All logic lives in `fork_join.py` so the concurrently developed
compensation module does not conflict.

## 3. Data model

```
ForkSpec(fork_id, branches=[BranchSpec...], join=JoinSpec, max_concurrent=None,
         failure_policy=None, cancel_siblings_on_branch_cancel=False)
BranchSpec(branch_id, capability, inputs={}, requires=(), produces=None, max_attempts=1, cost=None)
JoinSpec(mode="all"|"any"|"quorum", k=None, timeout_s=None, on_timeout="fail"|"partial",
         cancel_losers=True)
```

* `fork_id` and `branch_id` are caller-chosen stable identifiers (`[A-Za-z0-9_.-]{1,64}`), unique within the
  mission / fork. Branch order is the order of `branches` and is the scheduling order.
* **Branch node id** (token binding): `"{mission_id}:fork:{fork_id}:{branch_id}"`. It is a *real* `Node.id`
  (the executor, `_node` input and approval marker all key on it). Because the id embeds the mission, fork
  and branch, an approval token minted for branch A (bound to `node_id=A`) fails the existing fail-closed
  binding of PR #446 for branch B, for a different fork, and for a different mission. Capability and the
  input hash are also bound by the token, so a branch cannot be re-pointed.
* **Idempotency key**: `sha256("{mission}|{fork}|{branch}|{attempt}")[:32]` prefixed `fj-`. Same attempt ->
  same key across crashes, so operators dedupe; only a recorded failure + retry increments `attempt`.
* `ForkSpec` digest: sha256 of the canonical spec with input *hashes* (never input values), recorded in
  `ForkOpened`. Re-opening a fork id with a different digest is refused (replay divergence).

## 4. Durable events

All are mission-scoped (`mission_id` = parent). Payloads (additive; unknown keys ignored by readers):

| Event | Payload |
|---|---|
| `ForkOpened` | `fork_id, spec_digest, branches:[{branch_id,node_id,capability,operator,produces,inputs_sha256,max_attempts,cost}], join:{mode,k,timeout_s,on_timeout,cancel_losers}, failure_policy, limits:{max_branches,max_concurrent_branches,effective_concurrency,tenant}, opened_at, deadline` |
| `ForkRejected` | `fork_id, reason` (limit/validation/capability not granted; nothing ran) |
| `BranchStarted` | `fork_id, branch_id, node_id, attempt, idempotency_key, reserved:{usd,tokens}, resumed:bool` |
| `BranchSucceeded` | `fork_id, branch_id, node_id, attempt, result, receipt, late:bool` |
| `BranchFailed` | `fork_id, branch_id, node_id, attempt, reason, error, ran:bool, receipt?` (`ran:false` = refused before dispatch: budget, policy, admission) |
| `BranchCancelled` | `fork_id, branch_id, reason, was_running:bool` |
| `JoinSatisfied` | `fork_id, mode, k, winners:[...], failed:[...], cancelled:[...], abandoned:[...], partial:bool, reason` |
| `JoinFailed` | `fork_id, mode, k, reason, succeeded, failed, cancelled, failure_policy, decision:{action,note}` |

`receipt` = `{fork_id, branch_id, node_id, capability, operator, attempt, idempotency_key, input_sha256,
result_sha256, approved}`: it contains hashes only, never secrets or input values.

### Fold / replay rules (`fold_fork(events, fork_id)`; `fold_forks`)

1. Events for a fork are applied in log order; the fold is a pure function of the event list (no clock, no
   randomness). Folded branch lists are emitted in `ForkOpened.branches` order, never completion order.
2. Branch status: `BranchStarted` -> running (attempt n); `BranchSucceeded` / `BranchFailed` /
   `BranchCancelled` -> terminal **first terminal wins** per attempt. A later `BranchSucceeded` for a branch
   that is already cancelled/failed or whose fork is closed is kept as a `late_results` receipt (a real side
   effect happened and must stay visible) but never changes status or the join.
3. A `BranchStarted` with a higher `attempt` after a `BranchFailed` is a retry; same attempt twice (crash
   resume) is idempotent.
4. Parked: `NodeParked(node_id=<branch node>)` without a later `NodeResumed`/`NodeSkipped` -> `parked`.
5. `JoinSatisfied` / `JoinFailed` close the fork; the first wins, later ones are ignored.
6. Join evaluation (`evaluate_join`) is a pure function of terminal counts, so replay and live agree.

### How existing readers treat the events

| Reader | Treatment |
|---|---|
| `MissionRepository.node_status` / `node_results` | Branch events are not `Node*` events, so branches do not appear as plan nodes. The reused `NodeParked/Resumed/Skipped` for a branch node id add a `waiting` entry for that id only. |
| `pending_human` / needs-you / `gate_notify` / cockpit | See a parked branch through `NodeParked` (capability, packet, human task) unchanged. |
| `run()` completeness check | Compares only plan graph node ids; branch ids are ignored. A parked branch makes `pending_human` non-empty so a stalled `run()` reports `WAITING_HUMAN`. |
| `_saga` | Iterates `plan.graph.nodes` only. Branches are never auto-compensated. |
| `learning.py` / `receipt.py` / `observability.py` | Count `Node*` only; branch outcomes feed learning through `learning.record_capability` at commit (same call the node path makes). Branch receipts are exposed via `fork_summary`. Observability spans and `mission_receipt` rows for branches are a follow-up (deferred). |
| evidence producers (`compliance/evidence_producers.py`) | Approval evidence for a branch flows through the reused approval events. Branch events are ignored (additive). |
| Go store (`go/mission`) | Unknown types are ignored by every existing fold; `fork.go` folds the new events with the same rules and a shared golden fixture. |

## 5. Scheduling

Deterministic given the plan: branches are considered strictly in `ForkOpened` order; a branch starts when
(a) the effective concurrency has a free slot, (b) the tenant slot pool has capacity, (c) the capability's
resource keys (existing `concurrency.conflict`) and per-operator limit allow, (d) budget reservation succeeds.
A held branch stays in place (no reordering), so the *set and order of starts* is a function of the plan and of
completion events only. `BranchStarted` events are appended by the single coordinator thread before the
operator call is submitted. Outcome events are appended by the coordinator in completion order (which a thread
pool cannot make deterministic); the fold normalises this, and the winner of an ANY/QUORUM join is *recorded*
in `JoinSatisfied` so replay reads it rather than recomputing.

Limits and config (per tenant overrides the default; `Mission.tenant`, `""` = default tenant):

| Key | Default | Meaning |
|---|---|---|
| `max_concurrent_branches` | 4 | concurrent running branches for the tenant, across all its missions (shared slot pool) |
| `max_branches` | 16 | branches per fork; larger specs are `ForkRejected` |
| `max_open_forks` | 4 | simultaneously unresolved forks per mission |

Sources, lowest to highest precedence: `ForkLimits()` defaults, env `AGENTIC_OS_FORK_MAX_CONCURRENT`,
`AGENTIC_OS_FORK_MAX_BRANCHES`, `AGENTIC_OS_FORK_TENANT_LIMITS` (JSON `{tenant: {key: n}}`), explicit
`ForkLimits(per_tenant=...)`. Requested concurrency above the tenant cap is clamped (recorded); branch count
above `max_branches` is refused. A tenant's slots never admit another tenant's branch.

## 6. Join semantics

`n` branches, `s` succeeded, `f` failed, `c` cancelled, `r = n - s - f - c` still possible.

| Mode | Satisfied when | Failed when | On decision | Result handed on |
|---|---|---|---|---|
| `all` | `s == n` | first `f + c > 0` (fail-fast: no new branches start; in-flight branches drain and are recorded; then `JoinFailed`) | unstarted -> `BranchCancelled(join_failed)` | all results in plan order |
| `any` | `s >= 1` | `r == 0` and `s == 0` | `cancel_losers` (default true): unstarted and running losers cancelled; else drained | the recorded winner only |
| `quorum(k)` | `s >= k` (`1 <= k <= n`) | `s + r < k` | as `any` | the first `k` successes in plan order |

Timeout (`timeout_s`, measured from `ForkOpened.opened_at` on the injected clock, and never beyond the mission
deadline): at expiry stragglers are cancelled (`BranchCancelled(join_timeout)`), the join is evaluated over the
persisted outcomes, and `on_timeout` applies: `fail` (default) emits `JoinFailed(reason=timeout)`;
`partial` emits `JoinSatisfied(partial=true)` iff at least one branch succeeded (never for a zero-success fork).

Partial results: succeeded branch results are always in the log (`BranchSucceeded`) and in `fork_summary`,
whatever the join decision. Results are **published to world state** (`ObservationWritten` for `produces`,
after verification) only on `JoinSatisfied`, in plan order, for the branches named in `winners`. A failed join
publishes nothing, and the successful work remains visible (not deleted, not compensated).

## 7. Cancellation, budget, deadline

* Mission cancel: the controller subscribes to `MissionCancelled`; every open fork gets
  `BranchCancelled(mission_cancelled)` for unstarted and running branches and no new branch starts. Python
  cannot kill a running operator call: a call already in flight finishes and its outcome is recorded as a late
  receipt (section 4.2), so a consequential action that did happen is never invisible.
* Branch cancel (`cancel_branch`): only that branch, unless `cancel_siblings_on_branch_cancel` (join policy).
* Budget: `Mission.budget` is the only budget. Remaining = `budget.usd` - cost of DONE plan nodes - reserved
  by all branches of the mission, evaluated and decremented atomically under one lock together with the
  `BranchStarted` append. A branch whose estimated cost (spec cost, else capability cost) exceeds the
  remaining is refused: `BranchFailed(reason=budget_exceeded, ran=false)`, never partially run. A reservation
  is not refunded when the operator later fails (the spend is conservative). Tokens use the same mechanism.
* Deadline: `Mission.deadline` bounds `timeout_s`; an expired mission deadline behaves like a timeout.

## 8. Approvals, policy and saga

* On start each branch is checked with `context.resolve(CHECK_POLICY)` like a node. `denied` -> branch fails
  (`ran=false`), `required` -> `NodeParked` with the branch node id and the branch is `parked` (it does not
  hold a concurrency slot). The mission becomes `WAITING_HUMAN`. Approve/reject arrive via
  `MissionRuntime.approve(mission, branch_node_id, ...)` (authenticated by the caller as today); the hook
  records `ApprovalGranted{node_id, capability, fork_id, branch_id}` + `NodeResumed` and resumes the fork. A
  rejection records `ApprovalRejected` + `NodeSkipped` and fails that branch (`approval_rejected`); the join
  decides. Branch inputs are stripped of every `_approval*` / reserved key, so a branch cannot self-approve.
* An approval counts only while `ApprovalInvalidated` has not followed it and the capability still matches.
  On `PolicyChanged` with a revoke the controller emits `ApprovalInvalidated(policy_tightened)` for every
  branch that is parked or approved-but-not-started; an already running branch is unaffected (change_policy
  is non-retroactive by design). A branch whose capability is no longer in the mission's scoped registry at
  start time fails with `policy_revoked`, `ran=false`.
* Saga: `_saga` is untouched and does not see branches. Failure policy hook:
  `ForkJoinController.on_branch_failure(policy)` registers `policy(ctx) -> FailureDecision`; `ctx` carries
  `mission_id, fork_id, join_mode, reason, succeeded, failed, cancelled` (each branch with node id, capability,
  receipt) so the compensation agent can compensate exactly what it chooses. The default `HALT_AND_REPORT`
  returns `action="halt_and_report"`: the mission is failed through `_fail` (no saga), `outcome` lists
  succeeded and failed branches, and nothing is compensated. The policy is consulted once, when the join
  fails (after all outcomes are persisted). Actions: `halt_and_report` (default), `compensate` (controller
  does nothing itself; the plugged policy performs/queues compensation and the mission is failed), `continue`
  (mission left running for the caller; recorded in `JoinFailed.decision`). Policy exceptions fail closed to
  `halt_and_report`.

## 9. Crash recovery

`run_fork` is idempotent: given the same spec it folds the log and resumes. Closed fork -> returns the stored
result without calling any operator. Branch with `BranchSucceeded|Failed|Cancelled` -> never re-executed.
Branch started without an outcome (crashed in flight) -> re-dispatched with the **same attempt and
idempotency key** (`BranchStarted{resumed:true}`), which operators must honour (dedupe on key, as
`InMemoryOperatorClient` and the HTTP operator contract already do). The join is re-evaluated from persisted
outcomes. The test-suite simulates a crash before and after every appended event.

Recovery details: (a) a branch that was in flight when the process died is re-dispatched (same key) even when
the join was already decided as failed, so its outcome is drained and recorded rather than left unknown; when
the join was satisfied with `cancel_losers` it is cancelled and its effect state is unknown (flagged
`was_running`), exactly as a live cancel of an uncooperative call; (b) `run_fork` on a closed fork re-applies
only the idempotent mission effects (world publication keyed by `source_ref=node_id`, mission `_fail`), so a
crash between `JoinSatisfied/JoinFailed` and those effects heals on restart; (c) a rehydrated `Mission` does
not carry `tenant`, so the controller reads it from `MissionCreated`; (d) the original `ForkSpec` (inputs are
not in the log, only hashes) must be re-supplied to resume; a branch approval recorded while no spec is
registered resumes at the next `run_fork(spec)`.

## 10. Threat model

| Threat | Control |
|---|---|
| Branch escalates permissions | branch capability and `requires` must be in `_scoped(mission)` (mission grants); executor authority gate unchanged; no branch-supplied grants exist |
| Branch self-approves / forges approval | `_approval*` keys stripped from branch inputs; approvals only via `approve()`; HTTP tokens bound to mission+node+capability+inputs (PR #446) |
| Token for branch A runs branch B | node id embeds fork and branch; token verification is fail-closed on node id |
| Duplicate consequential action on retry/restart | deterministic idempotency key; `BranchStarted` persisted before dispatch; resume reuses the key |
| Sibling data poisoning | branch results are not visible to siblings until the join publishes; `$from_world` reads the pre-fork world |
| Resource exhaustion | per-fork `max_branches`, per-tenant `max_concurrent_branches`, `max_open_forks`, shared budget |
| Cross-tenant interference | slot pool, limits and fork ids are scoped by `Mission.tenant`; fork state is folded per mission id |
| Spec swap on replay | `spec_digest` mismatch is refused |
| Secrets in log | receipts and `ForkOpened` carry hashes; credential material never enters events (executor broker unchanged) |

## 11. Go port parity (phase 4)

Implemented: `go/mission/fork.go` folds the same events (`FoldFork`, `EvaluateJoin`) and a shared golden log
verifies Python == Go fold output; existing folds already ignore unknown types so replay does not choke.
**Deferred**: the Go `MissionRuntime` does not execute forks (controller, scheduler, budget, approve hook,
subscriptions); a Go runtime can replay and display fork state but cannot open one.

## 12. Collapse plan: enterprise executor copy

`agentic_os_enterprise/mission/executor.py` is a 71-line fork of the kernel executor
(`run` = attempts++ + `client.invoke`, `compensate` = unconditional undo with `:undo` key, 4-arg
`OperatorClient`). Once forks land it can be deleted in favour of the kernel `agentic_os.mission.executor.Executor`
if the enterprise runtime constructs the kernel class and passes these seams:

| Seam (kernel `Executor.__init__`) | Enterprise must supply |
|---|---|
| `client` | an `OperatorClient` accepting the `secrets=` keyword (kernel signature) |
| `broker` + `credentials_for` | Vault/OpenBao credential broker; `credentials_for(node)` from `CapabilityDescriptor.secrets` |
| `sandbox` + `isolation_for` | sandbox plane and the capability isolation class |
| `authority` / `authority_for` | the mission's leased `AuthorityContext` (per mission, via `secure_executor_for`) and `required_authority` |
| `monitor` | `SecurityMonitor` (needs `mission_id`, `observe`, optional `observe_credential`) |
| `evidence_probe`, `undo_registry` | wired by `MissionRuntime._executor_for` already; enterprise runtime inherits it |
| `_pre_exec_ok`, `_meter` | remain runtime overlay seams (`SUPPORTS_ADMISSION_SEAM`), used by forks unchanged |
| `idempotency_key` on `Node` | forks set it; enterprise client must dedupe on it |

Behavioural differences to resolve on collapse: kernel `compensate` is evidence-gated and mints a signed
marker (the fork's unconditional undo is the weaker behaviour and should go); `InMemoryOperatorClient` in the
fork lacks the `secrets` channel. Steps: (1) enterprise runtime builds the kernel `Executor` through
`secure_executor_for`; (2) run the enterprise suite against it; (3) delete the fork and re-export from the
kernel. Not done here (enterprise repo untouched).
