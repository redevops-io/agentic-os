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
| `IRREVERSIBLE` | declared irreversible (`irreversible="<note>"` or `compensation: {supported: false, reversibility: IRREVERSIBLE}`) | never compensated; listed in `not_reversible` |
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
capability("mail.send", handler, side_effecting=True, irreversible="a sent email cannot be recalled")
```

`irreversible` must carry a non-empty note (the string, or `compensation.notes`); a bare `True` fails the N10 lint.
Registration (`CapabilityRegistry.register`) **rejects**: a compensating target equal to the capability itself
(self-compensation would re-run the forward action under the compensation marker), `verify` equal to the forward
or compensating capability (it would run the undo twice), `supported: false` with `reversibility:
REVERSIBLE_EXPLICIT` or a named capability, `supported: true` with a non-reversible class, and
`supported` together with `irreversible`. The lint (`registry_problems`) additionally flags a nonexistent target,
a cycle (A's undo is B and B's undo is A), a `verify` that is unregistered or side-effecting, and a reversible
declaration with no `verify` read-back.

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

Two optional per-action fields (section 9): `disposition: "PRESERVE"` (this effect survives a failed workflow;
requires `supported: false`) and `preconditions: "<read-only capability>"` (is reversing still safe NOW?).

A `compensation` dict is validated fail-closed (unknown keys, `supported` without `capability`, unknown
`reversibility` raise); a malformed remote declaration is read as *undeclared* (=> irreversible).

## 2. Failure policy

`Mission.failure_policy` (`create_mission(..., failure_policy=...)`, recorded in `MissionCreated`):

| Policy | Behaviour |
|---|---|
| `HALT_AND_REPORT` | stop; compensate nothing; succeeded effects are reported (`not_compensated`, `not_reversible`) |
| `COMPENSATE_DECLARED` | compensate completed steps that DECLARE a compensating capability, reverse order; irreversible steps are never "compensated", only listed |
| `CONTINUE_SIBLINGS` | let sibling branches finish; compensate nothing automatically |

Resolution (`effective_policy`): the mission's own `failure_policy` (`create_mission(failure_policy=...)`), then
the template default (`templates.TEMPLATE_FAILURE_POLICY`), then the deployment-wide escape hatch
`AGENTIC_OS_DEFAULT_FAILURE_POLICY`, then **`HALT_AND_REPORT`**. **Nothing is undone automatically by default**
(sequential missions included); `COMPENSATE_DECLARED` is an explicit per-mission / per-template / per-deployment
opt-in, and even then only steps that declare a compensating capability are touched. Fork branches ignore the
template and deployment defaults (only the mission's own opt-in applies). An unrecognised value fails safe to
`HALT_AND_REPORT`. A succeeded sibling is never compensated unless the policy is `COMPENSATE_DECLARED`.
`ExecutionGraph.forks` (optional, default empty; the fork/join executor sets it) is the single source for "this
plan is forked" (`compensation_policy.is_forked`) used by `run_saga`.

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
`run()` never re-runs a `COMPENSATION_FAILED` mission. Interventions go through `POST
/missions/{mid}/compensation/{retry|approve|acknowledge|verify_manually}` (section 6) -> the guarded
`MissionRuntime.compensation_intervention` (there is no public `retry_compensation`).

Per node, append-only (each payload carries `node_id`, `capability`, `undo`, `idempotency_key` = `{key}:undo`):

| Event | Extra payload |
|---|---|
| `CompensationRequested` | `attempt`, `policy`, `reversibility` (idempotency key is `{key}:undo:{attempt}`) |
| `CompensationUndeclaredTarget` | warning: the declared undo is not a registered capability (it never receives the marker) |
| `CompensationApprovalRequired` | parked: `inputs_sha256`, `undo_inputs` (exact inputs shown to the approver) |
| `CompensationApproved` | `approver`, `reason`, `attempt`, `undo`, `inputs_sha256`, `at`, `exp` |
| `CompensationAcknowledged` | `actor`, `reason`, `at` (a human accepts that the forward effect remains) |
| `CompensationAuthorized` | `basis` (`evidence_bound_marker`), `forward_executed`, `forward_approved`, `requires_approval`, `registered_undo` |
| `CompensationExecuted` | `receipt` {`node_id`,`undo`,`idempotency_key`,`result_sha256`}, `result` |
| `CompensationVerified` | `method` (`readback`, or `manual` with `actor`/`reason`/`at`) |
| `CompensationFailed` | `reason` (`undo raised: ...`, `compensation_skipped`, `compensation_requires_approval`, `undo_not_registered`, `forward_not_executed`, `verification_failed: ...`), `result` |

Legacy events are kept for existing consumers: `NodeCompensated` is emitted **only when verified**,
`NodeCompensationFailed` alongside `CompensationFailed`. `fold_compensation(events)` folds per-node status
`requested | authorized | executed_unverified | compensated | failed` (unknown event types ignored).

Mission `outcome` after a failed mission: `failure_policy`, `compensated` (verified only),
`compensated_unverified`, `compensation_incomplete`/`compensation_remaining` (failed), `not_reversible`
(`{node_id, capability, reversibility}`), `not_compensated` (reversible, left by policy), and
`compensation_acknowledged`, and `rolled_back` - `true` only if at least one effect was verified compensated by a
real `readback` and nothing else is left over (manual verification and acknowledgement never make it true).
**A mission is never reported rolled back because a compensating action was merely attempted.**

## 4. Authorization (the compensation's OWN)

Compensation is a separate dispatch with its **own** idempotency key (`{key}:undo:{attempt}`), its own
authorization, its own receipt (`CompensationExecuted.receipt`) and its own verification, and it is dispatched
with `_node = {node}:undo` so a signed token is bound to the compensation, never to the forward step.

* **`requires_approval: false` / legacy `undo=`** (registered undos only): the merged evidence-bound marker (#447:
  the forward step demonstrably executed in this mission, `node.undo` equals the registry's declaration).
* **`requires_approval: true`**: the evidence-bound marker alone is NOT enough, and the forward step's approval is
  not reused. The saga **parks** the compensation: `CompensationApprovalRequired` + a needs-you item "Approve
  compensation of <node>" showing the exact undo and inputs. A human approves through the authenticated route
  (approver credential when `AGENTIC_OS_APPROVER_TOKENS` is set); `CompensationApproved` binds mission, node,
  UNDO capability, attempt and the SHA-256 of the exact inputs, with a 1-hour expiry.
  **Separation of duty:** with `AGENTIC_OS_APPROVER_TOKENS` set, a requester credential cannot approve (403). The
  forward approver's identity is recorded on `ApprovalGranted.approver`; when MORE THAN ONE approver credential is
  configured, that same identity is refused (409) when approving the compensation (identity granularity is
  `role:tenant`, so this only separates distinct approver identities; `admin` is exempt). With approver tokens
  UNSET there is no separation: any authenticated caller, including the requester, can approve - exactly as for
  `/approve`. `Executor.compensate`
  refuses (no operator call) if any of those differ (replay on a later attempt, expired, other mission, other
  node/undo, modified inputs); the dispatch then carries a freshly signed token for the undo capability with
  marker `own_approval: true`. An approval is single-attempt: a retry needs a new approval.
* An undo that is not the registry's declaration is refused at **plan time** (`MissionBlocked{reason:
  compensation_undeclared}`) and again at run time (`undo_not_registered`). An undo that names a capability nobody
  registered ("dangling") gets a `CompensationUndeclaredTarget` warning and is never invoked with the marker; after
  the sunset date **2027-01-01** (`STRICT_UNDO_SUNSET`; env `AGENTIC_OS_STRICT_UNDO_TARGETS=1/0` overrides) such a
  plan is refused at plan time. Kernel fleets still declaring dangling undos (`content.retract`,
  `crm.retract_opportunity`, `crm.delete_activity`, `revenue.retract_followup`) sit on a shrink-only allowlist.
* A target that equals the forward capability, or is itself a forward action of the same plan, is refused
  (`compensation_target_is_forward_action`), at registration, in the runner and in `Executor.compensate`.

## 5. Verification

After the compensating action succeeds, the runtime invokes the declared `compensation.verify` read-back
(`{verify_undo_of, result, undo_result}` under `{key}:undo:{attempt}:verify`). It verifies ONLY if the result is
not failure-shaped, contains an explicit `verified: true` AND echoes the observed state (`observed` or `digest`).
Verify capabilities are trusted code owned by the same operator as the forward capability; a handler that lies
cannot be detected generically. **Trust limit:** `observed`/`digest` are checked for presence only. They are NOT
compared with the logged forward-result digest or a declared expected state (no capability declares one yet), so
`rolled_back: true` means "the operator's own read-back said so", not an independent audit. Outcomes:

* read-back passes -> `CompensationVerified(method=readback)` + `NodeCompensated`; counted in `compensated`.
  This is the ONLY path to `rolled_back: true`.
* read-back fails/raises -> `CompensationFailed(verification_failed)`; `COMPENSATION_FAILED`.
* no `verify` declared (including every bare `undo=` capability) -> **`compensated_unverified`**:
  `CompensationExecuted` only, no `NodeCompensated`, not in `compensated`, a needs-you item, `rolled_back: false`.
  There is no operator-ack shortcut. A human can attest via `verify_manually` (`method=manual`, with actor/reason):
  it moves the node to `compensated` but never sets `rolled_back`.

Retries: each attempt `n` (counted in the log) uses fresh keys `{key}:undo:{n}` and `:undo:{n}:verify`, so an
idempotent operator that replayed a returned error cannot pin the mission forever. A per-(mission,node) lock
serialises concurrent callers; attempts are capped by `AGENTIC_OS_COMPENSATION_MAX_ATTEMPTS` (default 3), after
which only `acknowledge` resolves the node.

## 6. Surface

* needs-you (`/api/needs-you`): `compensation_approval`, `compensation_failed` and `compensation_unverified`
  items per node (`compensation_runner.attention_items`), plus `effects_left_in_place` for a FAILED mission whose
  policy left reversible effects (`not_compensated`) or irreversible ones (`not_reversible`) behind.
* `/approve` on a terminal mission or a node with no pending approval -> 409 (a bogus reject can no longer re-enter
  the saga). `cancel_mission` is refused (409) while compensation is parked/failed: use `acknowledge` /
  `verify_manually`, so the leftover effect and its needs-you item stay surfaced.
* Routes (under the default-deny gate): `POST /missions/{mid}/compensation/{retry|approve|acknowledge|
  verify_manually}` with body `{node_id?, reason?}`. Unknown/other-tenant mission -> 404; `approve`,
  `acknowledge`, `verify_manually` need the approver credential when `AGENTIC_OS_APPROVER_TOKENS` is set (a
  requester token gets 403); `retry` is refused to an approver-only credential; wrong state / nothing to do ->
  409. Actor, reason and time are recorded in the log.
* `explain()`: `compensation` (verified/unverified/failed fold), `not_reversible`, existing
  `compensation_incomplete`/`compensation_remaining`; the Compensation* events are in `steps`.
* `mission_receipt`: `compensation` section; `observability`: `compensation_failed` count; cockpit state chip.
* Replay/rehydrate: state, outcome (`outcome_from_log`) and `failure_policy` are rebuilt from the log. The effective default (template/env/HALT) is frozen into
  `MissionCreated.default_failure_policy` at creation, so a later env change never alters replay; a duplicated
  `CompensationRequested` (same attempt) is ignored by the fold and cannot exhaust the attempt cap.
* Go port (`go/mission`): folds `compensation_failed` and ignores `Compensation*` events (only `NodeCompensated`
  moves a node); the Go saga is unchanged (see migration notes).

## 7. Conformance

`tests/conformance/test_conformance_static.py::test_n10_*`: every side-effecting capability of every app must
declare `compensation` or `irreversible="<note>"`; declared targets must exist in the same operator, must not form
cycles, and `verify` must be a registered read-only capability. Shrink-only allowlists, each entry with a one-line
justification: `ALLOW_N10` (17 undeclared capabilities, most of them drafts/analysis mislabelled `side_effecting`
- follow-up: set `side_effecting=False` with tests, or add delete-draft capabilities), `ALLOW_N10_VERIFY`
(reversible compensations that have no read-back capability yet) and `ALLOW_DANGLING_FLEET_UNDOS`.

## 8. Migration notes

Behaviour changes in this PR: (1) sequential missions no longer undo anything by default; (2) a bare `undo=` is
no longer "verified" (it is `compensated_unverified`); (3) `COMPENSATION_FAILED` is a new state; (4) idempotency
keys are `{key}:undo:{attempt}`; (5) an undo naming an unregistered capability is not invoked.

**Explicit opt-ins added (so existing flows keep their saga):** templates `onboarding` and `deploy_app`
(`TEMPLATE_FAILURE_POLICY`); tests `test_mission::test_saga_compensation_on_failure`,
`test_kernel_verify_launcher_saga` (`_saga_rt`), `test_approval_token` (`_saga_http`); the Go port's `onboarding`
and `deploy_app` templates and its two saga tests. 
**Workflows that now SILENTLY KEEP their effects on failure (HALT_AND_REPORT default) - decide each explicitly:**

| Workflow / app | Effects kept on failure |
| --- | --- |
| `invoice_recovery`, `revenue_rescue` | `billing.cancel_subscription`, `books.reverse_entry` |
| `product_launch`, `teardown_app`, `cost_audit` | their side-effecting steps (declare/verify undos first) |
| `content_distribution`, `inbound_lead` | steps whose undo capability is unregistered (dangling) |
| edge-sentinel | `block_ip` |
| video | `video.unpublish` |
| enterprise | `compliance.revert_fix`; enterprise demo/pilot missions |

Opted in: `onboarding`, `deploy_app` and `sky_deploy` (`sky.launch`/`sky.serve` declare registered, reversible,
approval-gated undos `sky.down`/`sky.serve_down`; opted in for consistency with `deploy_app`). **Recommendation:**
make an explicit per-workflow decision - opt a template in (`TEMPLATE_FAILURE_POLICY` or `failure_policy=`) only
once its undo capabilities are registered, declared reversible and have a `verify`; otherwise leave it on
HALT_AND_REPORT. A FAILED mission that kept reversible effects lists them in `outcome.not_compensated`
(plus `not_reversible`) and raises an `effects_left_in_place` needs-you item.

Deployments that need the old behaviour everywhere set
`AGENTIC_OS_DEFAULT_FAILURE_POLICY=COMPENSATE_DECLARED` (they still get `compensated_unverified` for bare undos).
Fixtures that declared an undo for a capability they never registered now register it (`demo.build_fleet`,
`test_mission._saga_fleet`, the kernel saga fixture).

* **Enterprise fork executor** (separate repo, not edited here): (a) honour the `undo_registry`,
  `compensation_requires_approval` hooks and the `attempt=`/`approval=` arguments of `Executor.compensate`, never
  mint a compensation marker for `requires_approval` capabilities without a `CompensationApproved` record;
  (b) the fork inherits the kernel `run_saga` (it has no separate undo loop) - confirm it does not override
  `_saga`/`Executor.compensate`; (c) `replan.py:132` collapses
  `COMPENSATION_FAILED` to `'failed'` - map it to its own value (and treat it as terminal-with-attention);
  (d) `mission_ci.py` checks `state == MissionState.FAILED` only - use `state in (FAILED, COMPENSATION_FAILED)`;
  (e) treat `COMPENSATION_FAILED` as terminal in tenancy/lifecycle code (`MissionFinished`).
* **mission-sdk**: add `compensation=`/`irreversible=` (with note) to its capability builder, serialize them in
  `/capabilities`, and make `Mission.failure_policy` a create-time option.
* **Fork/join** (parallel-branches work): set `ExecutionGraph.forks`, call `decide_on_branch_failure` on a branch
  failure; do not compensate succeeded siblings itself.
* **Go port**: parity for the policy gate (`EffectiveFailurePolicy`, default HALT_AND_REPORT; nothing is
  compensated unless COMPENSATE_DECLARED). Still to port: declared-only filtering is by `Undo`, no
  verification/approval/events yet - it replays the Python events but does not emit them.
* Consumers of `MissionState` / `m.state == FAILED` must also handle `COMPENSATION_FAILED`.

## 9. Per-action selection, preconditions and the partially-completed outcome

`failure_policy` decides whether a mission MAY compensate; it is deliberately coarse. A deployment that wants
"a failed workflow does not imply its successful actions should be undone" selects PER ACTION on the capability
(`CompensationSpec`), never per workflow:

| `compensation` declaration | On a failed mission (policy `COMPENSATE_DECLARED`) | Listed as |
|---|---|---|
| `supported: true, capability, verify, preconditions, requires_approval: true` | reversal considered: preconditions evaluated, a human approves, undo runs, read-back verifies | `compensated` / `retained_effects` if it could not run |
| `supported: false, disposition: "PRESERVE", notes: "..."` | **never reversed**, whatever the policy (completed beneficial / security / compliance actions) | `not_reversible` (+ `disposition`), `retained_effects[reason=preserved_by_declaration]` |
| `irreversible="<note>"` / undeclared | never reversed | `not_reversible`, `retained_effects[reason=irreversible|undeclared_reversibility]` |

Under the default `HALT_AND_REPORT` nothing is reversed and every effect is retained (`left_by_policy`).
`PRESERVE` is checked inside the compensation runner itself, so it also holds for fork-branch compensation.
A reversal of a preserved effect is a NEW decision (a separately authorized forward action), not an undo.

**Preconditions** (`compensation.preconditions`): a registered, read-only capability. Before the reversal is
offered for approval, and again right before it executes (the world can change while an approval is pending),
the runner invokes it with `{precondition_for_undo_of, undo, result, _node: "{node}:undo:precondition"}`
(idempotency key `{key}:undo:{attempt}:precondition:{plan|exec}`). Only an explicit `holds: true` proceeds.
Anything else (`holds: false`, a failure-shaped result, a raise, an unregistered or side-effecting check) fails
CLOSED: `CompensationSkipped{reason}`, nothing executed, the effect is retained. A skipped reversal is not a
failed compensation: the mission ends `FAILED` (not `COMPENSATION_FAILED`) with a `compensation_skipped`
needs-you item; `retry` re-evaluates it, `acknowledge` accepts it.

**Full declaration.** `compensation.full_declaration_problems(cap)` lists what an owner can demand of every
compensatable action: `compensation_capability` (`capability`), `preconditions`, `authorization_policy`
(`requires_approval: true`), `verification_method` (`verify`). The `idempotency_key` is never author-supplied: the
runner always derives `{node.idempotency_key}:undo:{attempt}` (read-back `...:verify`, precondition
`...:precondition:{phase}`), so a retry never reuses a key and a duplicate delivery is deduplicated by the operator.

**Partially completed.** When a mission did not finish and effects remain, the outcome carries
`retained_effects` (`{node_id, capability, reason, detail}`; reasons: `preserved_by_declaration`, `left_by_policy`,
`irreversible`, `undeclared_reversibility`, `precondition_not_met`, `compensation_failed`,
`awaiting_compensation_approval`, `compensation_unverified`, `acknowledged`), `partially_completed: true` and
`incomplete_steps` (`{node_id, capability, status, error}`: the steps that never finished, to retry or escalate).
`rolled_back` is `false` whenever anything is retained. The same facts are appended to the durable log as
`EffectsRetained` (once per distinct state), so the execution history names every effect left behind and why;
`outcome_from_log` rebuilds the identical outcome on replay. The `effects_left_in_place` needs-you item carries them
(`decisions: manual_remediation | escalate`) for preserved, precondition-held and policy-left effects. The failed
step itself may have partly executed: its effect is unknown to the log and is not listed as retained.
