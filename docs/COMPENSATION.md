# Compensation (saga-style, explicit-only)

**Policy: `COMPENSATION = EXPLICIT_ONLY`.** The kernel never performs an automatic rollback and never assumes
an operation can be reversed. A consequential action DECLARES whether it supports compensation and names the
compensating capability. Compensation is a *new, separately governed forward action*, not an undo button.

Builds on the merged guard (#447: `Executor.compensate` fails closed) and token binding (#446, see
`docs/SECURITY_MODEL.md`, "Compensation authorization"). Code: `agentic_os/mission/compensation.py`
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
| `PRESERVE_ONLY` | `HALT_AND_REPORT` plus: ONLY capabilities that declare `disposition: "CLEANUP"` (a temporary lock/staging resource, with `preconditions` and an `authorization_policy`) are cleaned up; every other successful effect is kept (section 10). Default of the kernel templates `onboarding`, `deploy_app`, `sky_deploy` |
| `COMPENSATE_DECLARED` | compensate completed steps that DECLARE a compensating capability, reverse order; irreversible steps are never "compensated", only listed |
| `CONTINUE_SIBLINGS` | let sibling branches finish; compensate nothing automatically |

Resolution (`effective_policy`): the mission's own `failure_policy` (`create_mission(failure_policy=...)`), then
the deployment-wide `AGENTIC_OS_DEFAULT_FAILURE_POLICY` (an explicit deployment decision; it beats the template
table, so `COMPENSATE_DECLARED` there opts every workflow back in and `HALT_AND_REPORT` there also disables the
`PRESERVE_ONLY` cleanup), then the template default (`templates.TEMPLATE_FAILURE_POLICY`: the three kernel templates
are `PRESERVE_ONLY`), then **`HALT_AND_REPORT`**. **Nothing is undone automatically by default**
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

**Superseded by section 10:** the kernel templates `onboarding`, `deploy_app` and `sky_deploy` no longer opt in to
`COMPENSATE_DECLARED`; they default to `PRESERVE_ONLY`. The paragraph below records the original 0.3.x opt-ins.

**Explicit opt-ins added (so existing flows keep their saga):** templates `onboarding` and `deploy_app`
(`TEMPLATE_FAILURE_POLICY`); tests `test_mission::test_saga_compensation_on_failure`,
`test_kernel_verify_launcher_saga` (`_saga_rt`), `test_approval_token` (`_saga_http`). 
**Workflows that now SILENTLY KEEP their effects on failure (HALT_AND_REPORT default) - decide each explicitly:**

| Workflow / app | Effects kept on failure |
| --- | --- |
| `invoice_recovery`, `revenue_rescue` | `billing.cancel_subscription`, `books.reverse_entry` |
| `product_launch`, `teardown_app`, `cost_audit` | their side-effecting steps (declare/verify undos first) |
| `content_distribution`, `inbound_lead` | steps whose undo capability is unregistered (dangling) |
| edge-sentinel | `block_ip` |
| video | `video.unpublish` |
| enterprise | `compliance.revert_fix`; enterprise demo/pilot missions |

Opted in at the time (reversed in section 10): `onboarding`, `deploy_app` and `sky_deploy`. **Recommendation:**
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
* Consumers of `MissionState` / `m.state == FAILED` must also handle `COMPENSATION_FAILED`.

## 9. Per-action selection, preconditions and the partially-completed outcome

`failure_policy` decides whether a mission MAY compensate; it is deliberately coarse. A deployment that wants
"a failed workflow does not imply its successful actions should be undone" selects PER ACTION on the capability
(`CompensationSpec`), never per workflow:

| `compensation` declaration | On a failed mission (policy `COMPENSATE_DECLARED`) | Listed as |
|---|---|---|
| `supported: true, capability, verify, preconditions, authorization_policy` | reversal considered: preconditions evaluated, a human approves, undo runs, read-back verifies | `compensated` / `retained_effects` if it could not run |
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
(`HUMAN_APPROVAL`, which sets `requires_approval`; or an explicit `EVIDENCE_BOUND`, valid only for a
non-consequential mission-owned artifact such as a temporary lock this mission created; the two contradict
`requires_approval` the obvious way and are rejected), `verification_method` (`verify`). The `idempotency_key` is never author-supplied: the
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

**Review hardening (K1-K6).**
* A hand-built `CompensationSpec` INSTANCE is validated and normalised exactly like a dict (`coerce` returns a normalised
  copy): `authorization_policy="HUMAN_APPROVAL"` forces `requires_approval`; contradictory specs raise.
* `EVIDENCE_BOUND` never fronts an approval-gated undo: `CapabilityRegistry.register` rejects it in either registration
  order (and `registry_problems` lints it); the runtime additionally treats it as `requires_approval` (belt and braces).
  A consequential, money-moving or external reversal can only be `HUMAN_APPROVAL`. Legacy bare `undo=` is unchanged.
* `PRESERVE` wins over a stray legacy `undo=` (`declared_undo` is `None`; registration rejects the contradiction). The
  fork path honours and reports it: `partially_completed`, `retained_effects`, `EffectsRetained`.
* A remote (`/capabilities`) declaration that fails validation FAILS CLOSED: spec dropped, legacy `undo` ignored, the
  capability is read as irreversible.
* `outcome_from_log`: a node that is eligible for compensation but has no compensation record (truncated log, saga not
  run) is `not_yet_compensated` and keeps `rolled_back` false. `cancel_mission` of a mission holding a done
  side-effecting node settles the outcome and records `EffectsRetained`.
* Precondition invocations are bounded by `AGENTIC_OS_PRECONDITION_TIMEOUT_S` (default 30s); a timeout fails closed.
* Kernel apps: `sentinel.block_ip` (a completed security action) is now `PRESERVE`; lifting a block is the separate,
  approved `sentinel.unblock_ip`. `infra.provision/configure`, `sky.launch/serve` stay reversible+approval-gated. The
  kernel template defaults were decided by the owner and are now `PRESERVE_ONLY` (section 10); the enterprise overlay
  (`HALT_AND_REPORT`, enterprise `docs/COMPENSATION_POLICY.md`) remains stricter by also skipping the CLEANUP step.

## 10. Kernel template defaults: `PRESERVE_ONLY`

Owner decision (after 0.3.32; version not bumped here): **a failed workflow does not imply its successful actions should be undone.**
`templates.TEMPLATE_FAILURE_POLICY` sets `onboarding`, `deploy_app` and `sky_deploy` to `PRESERVE_ONLY`
(previously `COMPENSATE_DECLARED`).

**Behaviour on failure (no implicit rollback):**

| Template | Preserved (never auto-reversed) | Reported |
|---|---|---|
| `onboarding` | created accounts/subscriptions, configurations, completed setup steps | incomplete onboarding: `incomplete_steps`, `recovery_actions` |
| `deploy_app` | successfully applied/configured resources; a working deployment is NOT rolled back because verify (or a later step) fails | `retained_effects` (`left_by_policy`), `incomplete_steps` |
| `sky_deploy` | provisioned clusters/services and successful steps | `retained_costs`: each still-provisioned resource with identifiers and the estimated cost, or `cost_known: false` / `null` (never an invented number) |

**Cleanup of temporary resources stays possible.** `disposition: "CLEANUP"` (`CompensationSpec`) marks a preparatory
action whose effect is a temporary resource the mission itself created (lock, staging file/dir). Under `PRESERVE_ONLY`
only such actions are handed to the normal compensation runner, which still applies the per-action machinery: the
declared `preconditions` (independently safe NOW, fail closed), the declared `authorization_policy` (`HUMAN_APPROVAL`
parks for a human; `EVIDENCE_BOUND` only for a mission-owned artifact), its own idempotency key, receipt and
verification. `CLEANUP` is rejected at registration unless `supported`, `preconditions` and an authorization policy are declared,
and it cannot be combined with `incurs_ongoing_cost`. **A human approves every cleanup** (`HUMAN_APPROVAL`; the
approval prompt and needs-you item are titled "Approve CLEANUP of temporary resource ...") **unless** the UNDO
capability is registered, in the same registry, with the owner-side marker `mission_owned_temporary=True`
(`CapabilitySpec` / `capability(..., mission_owned_temporary=True)`, default false) and is itself not approval-gated,
cost-bearing or compensatable, the forward action is neither approval-gated nor cost-bearing, and the declaration says
`EVIDENCE_BOUND`. This is enforced in `CapabilityRegistry.register` (both orders; raises) and again at runtime (an
unmarked CLEANUP is forced to HUMAN approval). The marker is never read from a forward `compensation` dict (unknown
field: fail closed) nor from remote `/capabilities` JSON, so a remote operator's CLEANUP always needs a human. The precondition
runs even when the log lacks execution evidence (fail closed). A capability that declares nothing is never cleaned up. The kernel apps
declare no CLEANUP actions today (their effects are the deliverable); the mechanism exists for fleets that stage
temporary resources. `HALT_AND_REPORT` compensates nothing at all, CLEANUP included.

**Partial completion is visible.** Besides `retained_effects`, `incomplete_steps` and `partially_completed`, the outcome
(and the `EffectsRetained` log record and the `effects_left_in_place` needs-you item) now carries `completed_steps`
(steps still DONE; a cleaned-up temporary step is listed under `compensated` instead), `recovery_actions`
(`start_new_mission_for_remaining_steps`, `decide_explicit_compensation`, `manual_remediation_if_unwanted`) and, for
capabilities declaring `compensation.incurs_ongoing_cost` (`infra.provision`, `sky.launch`, `sky.serve`),
`retained_costs` = `{resources: [{node_id, capability, resource{cluster|service|...}, estimated_hourly_usd,
estimated_monthly_usd, cost_known, cost_source, action_to_stop_cost}], estimated_hourly_usd, estimated_monthly_usd,
unknown_cost_resources, complete}`. The estimate is read ONLY from the forward result (`sky.launch` echoes the
`hourly_usd`/`est_monthly_usd` of the approved `chosen` candidate from `sky.optimize`; it is an estimate, not a bill).
The kernel template flow does not thread `chosen` into `sky.launch`, so there the cost is reported as unknown unless a
deployment's planner supplies it; totals sum only the known entries and `complete` says whether any is unknown.

**Policy source and loud overrides.** `MissionCreated` records `default_policy_source` (`env` | `env_invalid` |
`template` | `default`; the outcome reports `mission` when the mission chose its own) and, when relevant,
`default_policy_warning`; the outcome carries both. A startup/creation-time log warning is emitted when
`AGENTIC_OS_DEFAULT_FAILURE_POLICY` is set and when it overrides a template default. An unrecognised env value is logged
at ERROR, recorded as `env_invalid` and still fails safe to `HALT_AND_REPORT` (which also drops the CLEANUP step).

**In-flight missions and replay.** The default is frozen into the mission at creation. Missions created on <= 0.3.32
carry `default_failure_policy=COMPENSATE_DECLARED` for these templates and therefore KEEP auto-reversing (with each
undo's own authorization) after the upgrade: this is deliberate, a running mission's rules do not change under it. The
runtime appends a one-time `LegacyFailurePolicyNotice` event, and the outcome (`legacy_policy_notice`) and the
`effects_left_in_place` item carry the note. An operator who wants them on the new default sets
`AGENTIC_OS_RERESOLVE_LEGACY_POLICY=1`: the next saga run records a `DefaultPolicyReresolved` event and `rehydrate`
re-applies it, so replay is deterministic and does not depend on the flag staying set; it only moves a mission toward
the current template default (it re-resolves only missions with a frozen COMPENSATE_DECLARED, no recorded
source and a PRESERVE_ONLY template today; a mission deliberately created with the env opt-in is indistinguishable).
Replay is faithful: a node with a compensation record in the log is reported from that record (`compensated`, ...)
whatever the current policy resolves to; only a node WITHOUT a record is `left_by_policy`.

**Costs.** The needs-you item has a human-readable `cost_summary` (an explicit "COST UNKNOWN" line when unknown). The
`sky.launch` estimate is withheld (unknown) when the launch failed over to another cloud. A FAILED or
rejected-after-dispatch `sky.launch` / `infra.provision` is listed as `possibly_provisioned` with cost unknown and the
identifiers its inputs carry.

**Retries.** A FAILED/COMPENSATION_FAILED mission is terminal for `run()` (no completed step is
re-dispatched); a restarted runtime rehydrates `done` nodes from the log and resumes only the unfinished ones; every
forward dispatch carries the node's stable idempotency key. Verified by counting-client tests outside the log
(`tests/test_deploy_mission.py`, `tests/test_onboarding_real.py`, `apps/sky/test_sky_deploy_preserve.py`). There is no
resume route for a FAILED mission (it is terminal; `resume()` only continues PAUSED): the recovery action is honestly
`start_new_mission_for_remaining_steps`, listing the remaining steps; completed steps are not repeated by the old mission.

**Compensation stays opt-in.** `create_mission(failure_policy="COMPENSATE_DECLARED")` for one mission, or
`AGENTIC_OS_DEFAULT_FAILURE_POLICY=COMPENSATE_DECLARED` for a deployment, restores the old behaviour, and each
reversal still needs its own authorization (`requires_approval` parks it; `sky.down`/`infra.destroy_delta` are
approval-gated).

### Migration table (0.3.32 -> next)

| Was | Now |
|---|---|
| `onboarding`/`deploy_app`/`sky_deploy` failure undid completed steps automatically (reverse order) | kept; reported as partially completed; undo only on explicit opt-in |
| tests asserting `CompensationExecuted` for these templates | assert preservation + `retained_effects`/`completed_steps`/`incomplete_steps`; separate explicit-opt-in tests keep the old assertions |
| env `AGENTIC_OS_DEFAULT_FAILURE_POLICY` lower priority than the template table | higher: it now also opts the kernel templates in (or out); source recorded, override warned |
| in-flight missions frozen at COMPENSATE_DECLARED | unchanged behaviour + `LegacyFailurePolicyNotice`; `AGENTIC_OS_RERESOLVE_LEGACY_POLICY=1` to re-resolve |
| outcome keys | + `completed_steps`, `recovery_actions`, `retained_costs`; `EffectsRetained` payload carries them |
| `CompensationSpec` | + `disposition: CLEANUP`, + `incurs_ongoing_cost` |
