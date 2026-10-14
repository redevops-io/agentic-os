# Compensation (saga-style, explicit-only)

**Policy: `COMPENSATION = EXPLICIT_ONLY`.** The kernel never performs an automatic rollback and never assumes
an operation can be reversed. A consequential action DECLARES whether it supports compensation and names the
compensating capability. Compensation is a *new, separately governed forward action*, not an undo button.

Builds on the merged guard (#447: `Executor.compensate` fails closed) and token binding (#446, see
`docs/APPROVAL_TOKENS.md`, "Compensation authorization"). Code: `agentic_os/mission/compensation.py`
(vocabulary, metadata, fold), `compensation_policy.py` (failure policy), `compensation_runner.py` (saga).

## 1. Vocabulary: reversibility classes

| Class | Meaning | Treated as |
|---|---|---|
| `REVERSIBLE_EXPLICIT` | a compensating capability is declared (`compensation.capability`, or legacy `undo=`) | may be compensated |
| `IRREVERSIBLE` | declared irreversible (`irreversible=True` or `compensation: {supported: false, reversibility: IRREVERSIBLE}`) | never compensated; listed in `not_reversible` |
| `UNKNOWN` | side-effecting, declared nothing | **irreversible**; listed in `not_reversible`; the N10 lint flags it |
| `NOT_APPLICABLE` | not side-effecting | n/a |

Capability declaration (`operator_sdk.capability(...)`, `CapabilitySpec`, remote `/capabilities` JSON):

```python
capability("res.reserve", handler, side_effecting=True,
    compensation={"supported": True, "capability": "res.release",   # undo= is derived from this
                  "reversibility": "REVERSIBLE_EXPLICIT",
                  "requires_approval": False,                       # see section 4
                  "verify": "res.check_released",                   # read-back capability (section 5)
                  "notes": "release the reservation"})
capability("mail.send", handler, side_effecting=True, irreversible=True)
```

Reference examples (kernel apps annotated where the capability exists):

| Action | Declaration |
|---|---|
| create draft -> delete draft (if safe) | `REVERSIBLE_EXPLICIT` (no app has a delete-draft capability yet; `social.draft`, `support.draft_reply`, `crm.draft_outreach` are on the N10 allowlist until one exists) |
| reserve resource -> release reservation | `REVERSIBLE_EXPLICIT` (`sky.launch`->`sky.down`, `infra.provision`->`infra.destroy_delta`) |
| add temporary block -> remove block (subject to policy) | `REVERSIBLE_EXPLICIT`, `requires_approval: true` (`sentinel.block_ip`->`sentinel.unblock_ip`) |
| send email | `IRREVERSIBLE` (`outreach.send_all`, `support.send_onboarding`, `billing.dunning`) |
| submit payment | `IRREVERSIBLE` - not automatically reversible (`billing.refund`); a refund/reversal is a new decision |
| publish externally | `IRREVERSIBLE` - not reliably reversible (`social.publish`, `assistant.push_assets`) |
| ledger entry / subscription | `books.record_revenue`->`books.reverse_entry`, `billing.create_subscription`->`billing.cancel_subscription` (approval-gated) |

A `compensation` dict is validated fail-closed (unknown keys, `supported` without `capability`, unknown
`reversibility` raise); a malformed remote declaration is read as *undeclared* (=> irreversible).

## 2. Failure policy

`Mission.failure_policy` (`create_mission(..., failure_policy=...)`, recorded in `MissionCreated`):

| Policy | Behaviour |
|---|---|
| `HALT_AND_REPORT` | stop; compensate nothing; succeeded effects are reported (`not_compensated`, `not_reversible`) |
| `COMPENSATE_DECLARED` | compensate completed steps that DECLARE a compensating capability, reverse order; irreversible steps are never "compensated", only listed |
| `CONTINUE_SIBLINGS` | let sibling branches finish; compensate nothing automatically |

Defaults (`effective_policy`): **sequential missions -> `COMPENSATE_DECLARED`** (today's reverse-order undo, now
restricted to declared compensations); **fork branches -> `HALT_AND_REPORT`**. An unrecognised value fails safe
to `HALT_AND_REPORT`. A succeeded sibling is never compensated unless the policy is `COMPENSATE_DECLARED`.

**Integration point for fork/join** (`agentic_os/mission/compensation_policy.py`, pure, no runtime imports):

```python
decide_on_branch_failure(mission, failed_branch, siblings) -> FailureAction
# FailureAction(kind=HALT|COMPENSATE|CONTINUE, policy, reason, compensate_nodes, not_reversible,
#               not_compensated, cancel_siblings)
```

Branches are duck-typed (attrs or dict keys): `id`, `status` (`succeeded|done|...`), `nodes` (each `id`,
`capability`, `side_effecting`, `undo`, `irreversible`). Only `succeeded` siblings (and the failed branch's own
completed nodes) are considered. `COMPENSATE` returns node ids in reverse order for the runtime to feed to
`run_saga`-style compensation; `HALT`/`CONTINUE` return the lists for the outcome. `run_saga` treats a plan
whose graph exposes a non-empty `forks` attribute as forked for the default policy.

## 3. State machine and events

New terminal/attention mission state **`COMPENSATION_FAILED`** (`MissionState.COMPENSATION_FAILED`). A mission
whose forward path failed ends in `FAILED` if nothing is left behind (no compensation attempted, or all verified)
and in `COMPENSATION_FAILED` if any compensating action failed, was refused or failed verification.
`MissionRuntime.retry_compensation(id)` re-attempts only the failed compensations (own idempotency key); it moves
the mission back to `FAILED` when none remain. `run()` never re-runs a `COMPENSATION_FAILED` mission.

Per node, append-only (each payload carries `node_id`, `capability`, `undo`, `idempotency_key` = `{key}:undo`):

| Event | Extra payload |
|---|---|
| `CompensationRequested` | `policy`, `reversibility` |
| `CompensationAuthorized` | `basis` (`evidence_bound_marker`), `forward_executed`, `forward_approved`, `requires_approval`, `registered_undo` |
| `CompensationExecuted` | `receipt` {`node_id`,`undo`,`idempotency_key`,`result_sha256`}, `result` |
| `CompensationVerified` | `method` (`readback` \| `operator_ack`) |
| `CompensationFailed` | `reason` (`undo raised: ...`, `compensation_skipped`, `compensation_requires_approval`, `undo_not_registered`, `forward_not_executed`, `verification_failed: ...`), `result` |

Legacy events are kept for existing consumers: `NodeCompensated` is emitted **only when verified**,
`NodeCompensationFailed` alongside `CompensationFailed`. `fold_compensation(events)` folds per-node status
`requested | authorized | executed_unverified | compensated | failed` (unknown event types ignored).

Mission `outcome` after a failed mission: `failure_policy`, `compensated` (verified only),
`compensated_unverified`, `compensation_incomplete`/`compensation_remaining` (failed), `not_reversible`
(`{node_id, capability, reversibility}`), `not_compensated` (reversible, left by policy), and
`rolled_back` - `true` only if at least one effect was verified compensated and nothing else is left over.
**A mission is never reported rolled back because a compensating action was merely attempted.**

## 4. Authorization

Compensation is a separate dispatch with its **own** idempotency key (`{idempotency_key}:undo`), its own
authorization (`CompensationAuthorized`), its own receipt (`CompensationExecuted.receipt`) and verification.
Decision (conservative reading): compensation never runs with weaker authorization than a fresh execution of
the compensating capability would need, **except** the merged evidence-bound marker for registry-declared undos
(#447: forward step demonstrably executed in this mission + `node.undo` equals the registry's declaration).
Concretely:

* default (`requires_approval: false` / legacy `undo=`): the evidence-bound marker applies (unchanged #447).
* `requires_approval: true`: the marker is minted only if the forward step itself ran under a human approval
  (`ApprovalGranted` not invalidated before success). Otherwise `Executor.compensate` refuses with
  `compensation_requires_approval`, no operator call is made, the saga records `CompensationFailed`, and the
  mission becomes `COMPENSATION_FAILED` with a needs-you item. The kernel apps' gated compensations
  (`billing`, `books`, `infra`, `sky`, `sentinel`) declare `requires_approval: true`; their forward steps are
  themselves approval-gated, so the normal saga still works.
* A planner-attached `undo` that is not the registry's declaration is refused at **plan time**
  (`validate_plan_compensation`; mission `FAILED`, `MissionBlocked{reason: compensation_undeclared}`, nothing
  dispatched) and again at run time (`undo_not_registered`).

## 5. Verification

After the compensating action succeeds, the runtime verifies via the declared `compensation.verify` read-back
capability (invoked with `{verify_undo_of, result, undo_result}` under `{key}:undo:verify`; passes when the
result is not failure-shaped and `verified` is not `false`). Outcomes:

* read-back passes -> `CompensationVerified(method=readback)` + `NodeCompensated`; counted in `compensated`.
* read-back fails/raises -> `CompensationFailed(verification_failed)`; `COMPENSATION_FAILED`.
* no `verify` declared -> **`compensated_unverified`**: `CompensationExecuted` only, no `NodeCompensated`,
  not in `compensated`, surfaced as a needs-you item, `rolled_back: false`.
* LEGACY exception (migration only): a capability with a bare `undo=` and no `compensation` declaration is
  verified by the operator's own successful result (`method=operator_ack`) so existing fleets keep working.
  Declaring `compensation` opts into the strict rule. Annotate to leave the legacy path.

## 6. Surface

* needs-you (`/api/needs-you`): `compensation_failed` (decisions `retry_compensation`, `acknowledge`) and
  `compensation_unverified` items per node (`compensation_runner.attention_items`).
* `explain()`: `compensation` (verified/unverified/failed fold), `not_reversible`, existing
  `compensation_incomplete`/`compensation_remaining`; the Compensation* events are in `steps`.
* `mission_receipt`: `compensation` section; `observability`: `compensation_failed` count; cockpit state chip.
* Replay/rehydrate: state, outcome (`outcome_from_log`) and `failure_policy` are rebuilt from the log.
* Go port (`go/mission`): folds `compensation_failed` and ignores `Compensation*` events (only `NodeCompensated`
  moves a node); the Go saga is unchanged (see migration notes).

## 7. Conformance

`tests/conformance/test_conformance_static.py::test_n10_*`: every side-effecting capability of every app must
declare `compensation` or `irreversible=True`, and a declared compensating capability must exist in the same
operator. `ALLOW_N10` lists the undeclared debt and may only shrink (stale entries warn). Kernel fleets
(`agentic_os/content/fleet.py`, `revenue/fleet.py`) declare `undo=` for capabilities that are not registered
(`content.retract`, `crm.retract_opportunity`, `crm.delete_activity`, `revenue.retract_followup`); that compensation
fails visibly (`COMPENSATION_FAILED`) rather than silently - implement or drop them.

## 8. Migration notes (not changed here)

* **Enterprise fork executor**: must (a) honour `compensation_requires_approval` and `undo_registry` hooks
  (`Executor.compensate` signature unchanged), (b) not mint a compensation marker for `requires_approval`
  capabilities without forward human approval, (c) call `run_saga`/emit the five Compensation* events instead of
  its own undo loop, (d) treat `COMPENSATION_FAILED` as terminal in tenancy/lifecycle code (`MissionFinished`).
* **mission-sdk**: add `compensation=`/`irreversible=` to its capability builder, serialize them in
  `/capabilities`, and make `Mission.failure_policy` a create-time option.
* **Fork/join** (parallel-branches work): call `decide_on_branch_failure` on a branch failure; do not
  compensate succeeded siblings itself.
* **Go port**: still auto-compensates any node with `Undo`; port `effective_policy` and the declared-only rule
  before relying on it for fork missions.
* Consumers of `MissionState` / `m.state == FAILED` must also handle `COMPENSATION_FAILED`.
