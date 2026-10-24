# Mission execution: fork/join and reconciliation

Parallel branches inside a mission, and how unknown outcomes (plan nodes and fork branches alike) are reconciled. Compensation is in [COMPENSATION.md](COMPENSATION.md).

## Contents

- [Mission fork/join](#mission-forkjoin) (was `MISSION_FORK_JOIN.md`)
- [Writing a reconciliation probe](#writing-a-reconciliation-probe) (was `RECONCILIATION_PROBES.md`)
- [Goal loop](#goal-loop)

## Mission fork/join

Status: implemented in `agentic_os/mission/fork_join.py` (Python spec) with event-folding parity in
`go/mission/fork.go`. Owner decision (binding): **forks are parallel branches within an existing Mission, not
independently authorized agents.**

### 1. Principles

* The parent Mission owns authority, budget, deadline and cancellation. A branch has none of its own.
* A branch is one capability invocation. It receives only the capability it declares (plus an explicit,
  mission-subset `requires` list); it can never widen the parent's grants.
* No branch approves anything. A branch that needs a human decision is parked through the same
  `NodeParked` -> `MissionRuntime.approve()` path as any node.
* Everything durable is an event. The fork state is a pure fold of the log; the join is evaluated only over
  persisted outcomes.
* A failed branch never invalidates a successful sibling's recorded work, and successful branches are not
  compensated automatically. The Mission's declared failure policy decides (default `HALT_AND_REPORT`).

### 2. Existing machinery that is extended (not duplicated)

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

### 3. Data model

```
ForkSpec(fork_id, branches=[BranchSpec...], join=JoinSpec, max_concurrent=None,
         stricter_failure_policy=None, cancel_siblings_on_branch_cancel=False)
BranchSpec(branch_id, capability, inputs={}, requires=(), produces=None, max_attempts=1)
JoinSpec(mode="all"|"any"|"quorum", k=None, timeout_s=None, on_timeout="fail"|"partial",
         cancel_losers=True)
```

* `BranchSpec` has NO cost: the budget reservation is always the capability's declared cost (a spec cannot
  lower it); a capability with a NaN/inf/negative cost is refused. `produces` must be exactly one of the
  capability's `provides`, and the published observation carries `source_type=fork_branch` and
  `source_ref=<branch node id>`.
* `fork_id` and `branch_id` are caller-chosen stable identifiers (`[A-Za-z0-9_.-]{1,64}`, full match), unique within the
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

### 4. Durable events

All are mission-scoped (`mission_id` = parent). Payloads (additive; unknown keys ignored by readers):

| Event | Payload |
|---|---|
| `ForkOpened` | `fork_id, spec_digest, branches:[{branch_id,node_id,capability,operator,produces,inputs_sha256,max_attempts}], join:{mode,k,timeout_s,on_timeout,cancel_losers}, failure_policy (effective mission policy), limits:{max_branches,max_concurrent_branches,effective_concurrency,tenant}, opened_at, deadline` |
| `ForkRejected` | `fork_id, reason` (limit/validation/capability not granted; nothing ran) |
| `BranchStarted` | `fork_id, branch_id, node_id, attempt, idempotency_key, reserved:{usd,tokens}, resumed:bool` |
| `BranchSucceeded` | `fork_id, branch_id, node_id, attempt, result, receipt, late:bool` |
| `BranchFailed` | `fork_id, branch_id, node_id, attempt, reason, error, ran:bool, receipt?` (`ran:false` = refused before dispatch: budget, policy, admission) |
| `BranchCancelled` | `fork_id, branch_id, reason, was_running:bool` |
| `JoinSatisfied` | `fork_id, mode, k, winners:[...], failed:[...], cancelled:[...], abandoned:[...], partial:bool, reason` |
| `JoinFailed` | `fork_id, mode, k, reason, succeeded, failed, cancelled, failure_policy, decision:{action,note,policy,compensate_nodes,not_reversible,not_compensated}` |
| `ForkDriverClaimed` / `ForkDriverReleased` | `fork_id, generation, owner, host, pid, lease_expires_at` / `fork_id, generation, owner` (fencing; section 9) |

`receipt` = `{fork_id, branch_id, node_id, capability, operator, attempt, idempotency_key, input_sha256,
result_sha256, approved}`: it contains hashes only, never secrets or input values.

#### Fold / replay rules (`fold_fork(events, fork_id)`; `fold_forks`)

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

#### How existing readers treat the events

| Reader | Treatment |
|---|---|
| `MissionRepository.node_status` / `node_results` | Branch events are not `Node*` events, so branches do not appear as plan nodes. The reused `NodeParked/Resumed/Skipped` for a branch node id add a `waiting` entry for that id only. |
| `pending_human` / needs-you / `gate_notify` / cockpit | See a parked branch through `NodeParked` (capability, packet, human task) unchanged. |
| `run()` completeness check | Compares only plan graph node ids; branch ids are ignored. A parked branch makes `pending_human` non-empty so a stalled `run()` reports `WAITING_HUMAN`. |
| `_saga` | Iterates `plan.graph.nodes` only. Branches are never auto-compensated. |
| `learning.py` / `receipt.py` / `observability.py` | Count `Node*` only; branch outcomes feed learning through `learning.record_capability` at commit (same call the node path makes). Branch receipts are exposed via `fork_summary`. Observability spans and `mission_receipt` rows for branches are a follow-up (deferred). |
| evidence producers (`compliance/evidence_producers.py`) | Approval evidence for a branch flows through the reused approval events. Branch events are ignored (additive). |
| Go store (`go/mission`) | Unknown types are ignored by every existing fold; `fork.go` folds the new events with the same rules and a shared golden fixture. |

### 5. Scheduling

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

### 6. Join semantics

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

### 7. Cancellation, budget, deadline

* Mission cancel: the controller subscribes to `MissionCancelled`; every open fork gets
  `BranchCancelled(mission_cancelled)` for unstarted and running branches and no new branch starts. Python
  cannot kill a running operator call: a call already in flight finishes and its outcome is recorded as a late
  receipt (section 4.2), so a consequential action that did happen is never invisible.
* Branch cancel (`cancel_branch`): only that branch, unless `cancel_siblings_on_branch_cancel` (join policy).
* Budget: `Mission.budget` is the only budget. Remaining = `budget.usd` - cost of DONE plan nodes - reserved
  by all branches of the mission, evaluated and decremented atomically under one lock together with the
  `BranchStarted` append. A branch whose estimated cost (the capability's declared cost) exceeds the
  remaining is refused: `BranchFailed(reason=budget_exceeded, ran=false)`, never partially run. A reservation
  is not refunded when the operator later fails (the spend is conservative). Tokens use the same mechanism.
* Deadline: `Mission.deadline` bounds `timeout_s`; an expired mission deadline behaves like a timeout.

### 8. Approvals, policy and saga

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
* Failure policy (single source = the MISSION): when a join fails the controller calls PR #450's
  `compensation_policy.decide_on_branch_failure(mission_policy, failed_branch, succeeded_siblings)` with the
  mission-level `failure_policy` recorded in `ForkOpened` (so replay is stable) and maps the `FailureAction`
  explicitly: `HALT` -> `halt_and_report`, `COMPENSATE` -> `compensate`, `CONTINUE` -> `continue`. The default
  for a forked mission is `HALT_AND_REPORT`. A `ForkSpec` may only choose `stricter_failure_policy`
  (`HALT_AND_REPORT`, or equal to the mission's policy); anything looser is `ForkRejected`.
  `on_branch_failure(hook)` is an optional extra hook that can only tighten the decision.
  * `HALT_AND_REPORT`: stop starting branches, DRAIN in-flight siblings and keep their work, compensate
    nothing, report succeeded branches as `not_compensated` and consequential ones without a declared undo as
    `not_reversible`. (#450's `FailureAction.cancel_siblings` is not used to cancel completed or in-flight
    work; completed work is never cancelled.)
  * `PRESERVE_ONLY`: like `HALT_AND_REPORT`, but succeeded branches whose capability declares `disposition=CLEANUP`
    (temporary resources, with preconditions + authorization) are compensated; everything else is kept.
  * `COMPENSATE_DECLARED`: the controller runs `compensation_runner._compensate_node` for each succeeded
    branch the policy named (reverse order), so every one gets its own `CompensationRequested/Authorized/
    Executed/Verified|Failed` events and receipt, under `{branch idempotency key}:undo`, once (folded status
    skips repeats, including after a crash). Authorization evidence for a branch node comes from
    `BranchSucceeded` (non-late) via `MissionRuntime._compensation_evidence`. Irreversible/unknown branches are
    reported `not_reversible`; a failing undo leaves the mission in `COMPENSATION_FAILED`; a failed call that
    may have applied a consequential effect is reported `UNKNOWN_EFFECT`, never as rolled back.
  * `CONTINUE_SIBLINGS`: mission left running for the caller; recorded in `JoinFailed.decision`.
  The fork registers itself on `plan.graph.forks` (the single source for "forked" used by #450's
  `run_saga`); `_saga` still only walks plan nodes.
* Retries: a retry gets a new idempotency key, so after a failed real call a side-effecting capability is
  retried only when the failure carries `not_applied=True` (exception attribute); otherwise the branch fails
  and is reported (`UNKNOWN_EFFECT`).

### 9. Crash recovery

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

Single driver: `run_fork` holds a process-wide lock per (log, mission, fork) for the whole drive, and writes
a durable `ForkDriverClaimed{generation, owner, host, pid, lease_expires_at}` (under an flock on
`<log>.forklock` for file logs, after `EventStore.refresh()`); a second PROCESS gets `ForkError` while the claim
is live (same-host pid alive and lease unexpired, or lease unexpired elsewhere; in-process claims are live
only while their drive loop runs). `BranchStarted` carries the claim `generation`. A `running` branch not in
this driver's futures is re-dispatched only if its generation is OLDER than the current claim (a detected
restart), never from a concurrent driver. Leases are renewed in the drive loop; `ForkDriverReleased` is written
on exit. Limitation: the fence is only as strong as the log (JSONL + flock on one host); a multi-host
deployment needs a store with compare-and-set on the claim.

Restart: the spec (inputs are not logged) must be re-supplied. `run_fork(spec)` or
`register_spec(mission, spec)` validate it against the logged `spec_digest` (`spec_drift` refused);
`approve()` resumes the fork only when a matching spec is registered. `MissionRuntime.run()` does NOT drive
forks, and there is currently NO API route that opens or drives a fork: the #444 gate and tenant isolation are
therefore N/A until one exists, and any future route MUST sit behind the gate.

### 10. Threat model

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

### 11. Go port parity (phase 4)

Implemented: `go/mission/fork.go` folds the same events (`FoldFork`, `EvaluateJoin`) and a shared golden log
verifies Python == Go fold output; existing folds already ignore unknown types so replay does not choke.
**Deferred**: the Go `MissionRuntime` does not execute forks (controller, scheduler, budget, approve hook,
subscriptions); a Go runtime can replay and display fork state but cannot open one.

### 12. Collapse plan: enterprise executor copy

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

## Writing a reconciliation probe

When a dispatched action's outcome is unknown, the supervisor asks the provider, read-only, whether the effect exists. **You write the
adapter; the supervisor decides what to believe.** Read this before you write one.

### The one rule

Only positive execution evidence or an authoritative negative resolves an unknown outcome. Repeating your read-only probe is fine.
Repeating the business action while the outcome is unknown never is, and running out of probe retries never changes that.

### What you return

A `ProbeResult`, never a boolean:

| `outcome` | Meaning | What the supervisor does |
|---|---|---|
| `CONFIRMED_EXECUTED` | The effect exists. Attach `evidence` (kind + a checkable reference), the `provider_ref`, and the real `result` | settles the reservation, restores the result, resumes the mission |
| `CONFIRMED_NOT_EXECUTED` | The effect provably does not exist | accepted **only** if you qualified the adapter for it (below); then releases, re-gates and retries the action once |
| `INCONCLUSIVE` | Not observed / cannot tell | retries the probe with backoff |
| `TRANSIENT_ERROR` | The lookup failed (timeout, 429, 5xx). Set `retry_after_s` if the provider sent Retry-After | retries the probe with backoff, honouring Retry-After (bounded) |
| `UNSUPPORTED` | This provider cannot be reconciled reliably | escalates to a person immediately |

A generic HTTP 404 is `INCONCLUSIVE`: "not found" means "not observed". Use `from_http_status(status)` for the standard mapping.

### What you declare: `Qualification`

```python
Qualification(
    name="ledgerco",
    eventually_consistent=True,      # a fresh effect may not be visible yet (the default: assume it is)
    visibility_window_s=600,         # the provider's documented maximum delay before a fresh effect is visible (None = unknown)
    authoritative_negative=False,    # may an absence from THIS lookup prove the effect did not happen? (the default: no)
)
```

A `CONFIRMED_NOT_EXECUTED` is accepted only when **all** hold, enforced by the supervisor and not by you:

1. the adapter was registered with `authoritative_negative=True`;
2. the result itself says `authoritative_negative=True` (necessary, never sufficient);
3. evidence is attached;
4. if the provider is eventually consistent: it documents a `visibility_window_s` and that window has elapsed since the dispatch.

Anything else is downgraded to `INCONCLUSIVE` and retried. Say `authoritative_negative=True` only for a **strongly consistent lookup by the idempotency
key we sent** (so "zero results" really means "never created"). A search index, a list endpoint with paging, or anything that can lag is not.

If you document a visibility window, no probe is scheduled before it: probes before the window cannot say anything useful.

### Registering

```python
registry = ProbeRegistry()
registry.register("billing.*", my_probe, Qualification("lago", eventually_consistent=False, authoritative_negative=True))
supervisor = ReconciliationSupervisor(runtime, ReconcileJobs(path), registry, policy=PRESETS["default"], notifier=page_someone)
supervisor.tick()        # call it on a schedule; every transition is durable
```

Two adapters for one capability must agree: one saying executed and another saying not executed is conflicting evidence and escalates immediately.

### Retry policy

`RetryPolicy(max_probes=5, initial_s=30, max_s=300, factor=2, jitter=0.2)`: probes at about 30 s, 1 m, 2 m, 4 m, 5 m, with jitter. These are starting values, not
provider guarantees; presets `fast`, `default` and `slow` are provided. Exhausting the budget escalates to a person; it does not resend.

### Escalation and notification

An escalation is recorded once (durably) and the notifier is called with a stable idempotency key (`escalation:<job>:<time>`). Delivery is retried a bounded
number of times if the notifier fails; a receiver should dedupe on the key, because a crash between a successful send and recording it can send once more.

### Fork branches

A fork branch follows the same contract as a plan node. If its call may have been sent and its outcome is unknown (an ambiguous error on a side-effecting capability, or a driver lost with the call in flight), it writes the same `ReconciliationRequired` record (with `fork_id` and `branch_id`), its reservation
stays UNKNOWN, and the mission parks in `WAITING_RECONCILIATION`. Your probe, the supervisor, `reconcile_node` and the inbox need nothing fork-specific: the node id is the branch node id (`<mission>:fork:<fork>:<branch>`), and `ProbeContext.lease` is the branch attempt's reservation.

What resolution does to the fork: **executed** completes the branch from the evidence (return the provider's real `result`; a branch that `produces` a fact requires it) and the same fork carries on, without rerunning branches that succeeded; **authoritative not-executed** makes the next attempt a
new one under a fresh reservation if the branch has attempts left, otherwise the branch fails cleanly (not as an unknown effect). After a process restart the owner re-supplies the fork spec (`forks.register_spec`, then `run_fork`), because branch inputs are not in the log; until then the mission stays PAUSED.

## Goal loop

`agentic_os/mission/goal_loop.py` pursues a goal across iterations; each iteration is an ordinary mission, so every gate, reservation and reconciliation rule applies unchanged.

```python
loop = GoalLoop(runtime, driver, store=EventStore(path), verifier=check_goal, leases=MissionLeases(db))
lid = loop.start(LoopSpec("onboard acct 42", limits=HardLimits(max_iterations=6, max_usd=20)))
loop.run(lid)      # drive; call again any time (restart, after a reconciliation, after an escalation is resolved)
loop.tick()        # a scheduler: continue every loop whose mission moved
```

* `driver.start(ctx)` creates (does not run) the iteration's mission and returns its id; `ctx.completed` holds the step signatures already done so it can skip them. `driver.replan(ctx)` returns guidance for the next iteration or `None` (no different approach: the loop escalates).
* `verifier(ctx, mission)` returns `Verification(verified, evidence, reason)`. Evidence is validated like a reconciliation's; without a verifier the loop ends `completed_unverified`.
* Hard limits stop the loop (`limit`); only `extend_limits(actor=..., reason=...)` raises them. Soft thresholds (`SoftThresholds`) diagnose, replan a bounded number of times, then escalate; `resolve_escalation(actor=..., decision="continue"|"stop", reason=...)` decides.
* An unresolved reconciliation holds the loop (no new iteration, no replan). An authority denial, a mission-level ceiling, an unknown effect and a failed compensation escalate immediately.
* Wait states: `classify_loop(state)`; only a quiet RUNNING loop is watchdog-eligible. Sentinel events go to `on_event`: LOOP_STALLED, BUDGET_NEAR_LIMIT, LOOP_ESCALATED, LOOP_LIMIT_REACHED, LOOP_COMPLETED, LOOP_CANCELLED.
* In the production profile leases are required and the runtime's store must be a `FencedEventStore`. A leased mission can only be written by its lease holder, so a console that reconciles or cancels one takes the lease first.

### Direct gateway calls

The same rule holds for a side-effecting DIRECT capability called through the agent gateway: if an earlier attempt (same authority key, capability and idempotency key) was sent and its outcome is unknown, the next attempt is refused (`authority:outcome_unknown`) until a person resolves it with `AgentGateway.reconcile_call(lease_id, executed=..., evidence=..., actor=..., reason=..., result=...)`. `executed=True` with a `result` makes a retry with the same key return that result; `executed=False` returns the reservation. `unresolved_calls(key)` lists what is waiting. In the production profile a side-effecting direct call must carry an idempotency key. An agent that changes the key looks like a new action: derive the key from the call (tool and arguments) in the tool wrapper; the grant limits and the allowance bound what a fresh key can do.

