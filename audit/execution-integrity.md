# Workstream C: execution integrity audit (read-only)

Repo: /Users/arybach/agentic-os, branch audit/agent-security-reliability-2026-10-23. Date 2026-10-23.
Labels: CONFIRMED (read code and/or ran it), INFERRED (code reading only), UNTESTED. Severity P0 (can cause an unrecorded or duplicate real-world side effect or false "done" in a production path), P1, P2.
Paths below are relative to /Users/arybach/agentic-os/agentic_os unless stated.

Tests run (venv-ent, py3.12, offline, fsync off): tests/test_operator_sdk.py test_mission.py test_integration_phase1_events.py test_revenue_restart.py test_reliability_order.py test_verification.py test_mission_concurrency.py test_compensation_authorization.py test_rehydrate_outcome.py test_event_log_reproducibility.py test_approval_token.py test_integration_obligation.py test_experiment_payout_reconciliation.py: 153 passed. The placeholder-guard test (a copy of agent-harness-dev, run from the scratchpad) passed 25/25. Probe script: scratchpad/probe_c.py.

## 1. Summary of headline gaps

| # | Gap | Sev | Label |
|---|-----|-----|-------|
| C1 | No UNKNOWN / "outcome ambiguous" state anywhere in the mission or Today lifecycle. A timeout after a possible side effect becomes FAILED (mission) or a stuck item (Today). No reconcile-before-retry step. | P0 | CONFIRMED (probe) |
| C2 | Verification is not independent. `verify_result` and `CompositeVerifier` judge the provider's or operator's own returned dict. A fabricated `artifact_refs` string yields SUCCEEDED + delivery_verified. | P0 for any real adapter (the only adapter today is the fake) | CONFIRMED (probe) |
| C3 | Idempotency/dedupe stores are in-memory per process (Operator `_seen`, `ReplayGuard`, `Executor._ledger`, `EventInbox._seen`, `TaskManager._by_idem`). Crash between NodeDispatched and NodeSucceeded re-dispatches the node, and the dedupe cache is gone. | P0 once more than one process or a restart is involved | CONFIRMED by code; replay not executed |
| C4 | Exceptions and failure-shaped results are never cached by design (`operator_sdk.py:223`). The runtime has no retry or timeout policy and no UNKNOWN. A timeout after commit followed by a manual or app-level retry re-executes the side effect. | P1 | CONFIRMED |
| C5 | Receipts and the event log are not tamper-evident (no hash chain, no signature). | P1 | CONFIRMED |
| C6 | Trace recorder stores full, unredacted prompts, tool args and tool results in plain blobs with a 64-bit truncated hash. | P1 | CONFIRMED |
| C7 | Placeholder-contamination guard exists with a regression test, but only on the unmerged branch `fix/context-correctness-observability` of agent-harness-dev. It is absent from the agent-harness main/ours-h2 checkout. It is bypassed by nested args and by a stub not at the start of a string. | P1 | CONFIRMED |
| C8 | Deadlines: `reliability.py` ExpectedTransition/monitor_transitions is wired only into commercial/order.py. Mission runtime, Today and the executor have no deadline, timeout or cancellation of in-flight work. | P1 | CONFIRMED |
| C9 | No requested-artifact-format verification (type, extension, magic bytes) on the completion path. | P1 | CONFIRMED (absence) |
| C10 | Approval in the mission path is bound to node+capability, not to the inputs the human saw. It has no expiry (GateExpired is notice-only). | P1 | INFERRED |

## 2. Lifecycle states

Requested: PROPOSED, AUTHORIZED, AWAITING_APPROVAL, EXECUTING, VERIFYING, SUCCEEDED, FAILED, BLOCKED, UNKNOWN. No single state machine carries them. Four partial ones exist.

- MissionState (mission/types.py:37-49): PLANNING, SIMULATING, RUNNING, WAITING_HUMAN, PAUSED, FAILED, SUCCEEDED, CANCELLED, COMPENSATION_FAILED.
- NodeState (types.py:52-60): PENDING, READY, RUNNING, WAITING, DONE, FAILED, SKIPPED, COMPENSATED. `node_status` (mission/store.py:296-312) folds NodeDispatched to RUNNING and NodeSucceeded to DONE.
- Today (today.py:34-35): STATES `needs_action|waiting|scheduled|running|verified|failed|dismissed`. LIFECYCLE `suggested, approved, sent, delivery_verified, outcome_observed`. This is the best-designed one. It keeps sent and verified apart on purpose.
- External task (agent_gateway/external/contracts.py:~78): TaskState, with WAITING_FOR_INPUT, WAITING_FOR_APPROVAL, terminal states, and a `CapabilityStatus.UNKNOWN` (capability support, not outcome). Receipts carry status SUCCEEDED, FAILED or HELD.

| Requested | Exists? | Where / note |
|---|---|---|
| PROPOSED | partial | Today "suggested"; `CandidateStatus.PROPOSED` in projects/contracts.py:143 (a different concept). Mission has PLANNING. |
| AUTHORIZED | partial | Event `ApprovalGranted` and `PolicyEvaluated`; no state. Today "approved". |
| AWAITING_APPROVAL | yes | `WAITING_HUMAN`; `NodeState.WAITING`; `pending_approval` results. |
| EXECUTING | yes | `RUNNING`; NodeDispatched. |
| VERIFYING | missing | Verification runs inline in `_finish` (runtime.py:~820). No persisted state or event between dispatch and result. An ESCALATE parks as WAITING. |
| SUCCEEDED | yes | |
| FAILED | yes | Conflates "did not happen" with "may have happened" (see UNKNOWN). |
| BLOCKED | partial | `_block_denied`; `AttentionKind.BLOCKED` in world/attention.py:32; bootstrap.BLOCKED (device posture). Not a node state. |
| UNKNOWN | missing | The only UNKNOWNs are observation `KnownAtQuality.UNKNOWN` (observation.py:32), social `Signal.UNKNOWN`, capability-status UNKNOWN, and `integration/provider.py:32`. None is an outcome state. verify.py FAILED_STATUSES even lists "timeout", "partial" and "unavailable" as failure. |

Scenario evidence for the UNKNOWN gap:
- Mission path: any operator exception, including a `BridgeError` from a timeout (mission/bridge.py:75-76, which drops the cause with `from None`), goes to `_fail_node` (runtime.py:792-802). It appends NodeFailed, runs the saga (compensation), and sets the mission FAILED. Nothing records that the effect may have happened. The saga may run an undo for an effect that did not occur. `Executor.compensate` skips when the forward step has no executed evidence, which is safe, but the remote timeout case leaves a real effect plus a FAILED mission.
- Today path (probe, CONFIRMED): an adapter that commits and then raises TimeoutError out of `submit_task`. The exception escapes `Today.approve` (today.py:486, not wrapped). Resulting item: state `needs_action`, lifecycle approved=True, phase `suggested`, sends=0. A retry with the same digest raises `ApprovalError: approval already consumed` (approval_bridge.py:115), which is not a TodayError. Provider effects: 1. Outcome: no duplicate, but the item is stuck, shows the wrong state, and has no reconcile path. Only `status != SUCCEEDED` on a returned receipt is handled (today.py:502-507, "Not confirmed sent — check before retrying"). That is a HELD receipt, mapped to "failed", and `attempt += 1` makes a fresh approval produce a new idempotency key `f"{item_id}:{c.attempt}"` (today.py:325). The text tells the human to check, but nothing enforces it. A re-approval after an unconfirmed send issues a new task and can double-send.

Proposed fix (C1, P0):
1. Add `ExecutionOutcome.UNKNOWN` (a NodeState and a receipt status).
2. Classify transport timeout, 5xx after send, and process crash with a NodeDispatched marker but no result as UNKNOWN. Never FAILED, and never run the saga on UNKNOWN.
3. Park the node as BLOCKED/WAITING with a `reconcile` task. The task calls a read-only provider lookup by idempotency key or external id.
4. Only the reconcile result (found => SUCCEEDED, absent => FAILED, retry allowed) may move the node.
5. In Today, wrap `approve` in try/except, record phase `unknown`, and refuse re-approve until `reconcile()` has run.

## 3. Idempotency, retries, timeouts, concurrency, checkpoint/resume, deadlines, cancellation, compensation

**Dispatch idempotency.**
- The key is `sha256(mission:revision:outcome)[:16]` (mission/compiler.py:46-47). It is stable per node across restart and revision, and it is sent as an `Idempotency-Key` header and body field (mission/operators.py:48-74, bridge.py:70).
- Operator-side dedupe (mission/operator_sdk.py:146-184) is on `(capability, canonical inputs, key, secret ctx)`. It has in-flight coalescing: concurrent identical calls wait and share one execution (300s wait, then TimeoutError). Covered by tests/test_operator_sdk.py (`test_operator_invoke_dedupes_on_idempotency_key`, `test_idempotency_key_scoped_to_capability_and_inputs`, `test_approved_retry_is_not_served_the_cached_staged_result`, `test_tenant_tokens_do_not_share_idempotent_results`). CONFIRMED passing.
- Limits (CONFIRMED by code):
  - The cache is in-memory and LRU-bounded (cap 10000, `AGENTIC_OS_IDEMPOTENCY_MAX`). `ReplayGuard` for approval nonces is per-process and carries the comment "SEAM: per-process; multi-replica needs a shared store" (approval.py:180-204), with `warn_if_replicated`. A restart, a second replica, or LRU eviction means the same key re-executes the handler. The key is not honoured by the real provider unless the handler passes it through.
  - Only successes are cached. Exceptions and failure-shaped results are not (operator_sdk.py:221-230). That is correct for "retry after a clean failure" but wrong for "ambiguous".
  - The in-process mission `Executor._ledger` (executor.py:~190) exists only for compensation evidence.
  - The InMemoryOperatorClient dedupe (executor.py:42-62) is a test double.
  - The mission-level `_run_lock` is a `threading.RLock` per process (runtime.py:627-643). The Postgres event store is shared across instances (event_backends.py:110), but no cross-process lock or lease protects `run()`. Two instances can fold the same log and both dispatch a ready node. INFERRED, P0 for multi-instance deployments. Fix: DB advisory lock or lease row per mission, and a conditional append `NodeDispatched` with a unique (mission, node, attempt) constraint, executed before invoke.

**Crash between dispatch and result (checkpoint/resume).** `NodeDispatched` is appended before invoke (runtime.py:785, 909). After a crash `node_status` returns RUNNING, which is not in `_TERMINAL` (runtime.py:53). `run()` (runtime.py:645-711) rebuilds `done` from terminal states only, so the RUNNING node returns to the scheduler frontier (scheduler.py:43-49 only skips done/running IDs passed in, and `running=set()` is passed) and is re-dispatched with the same key against a fresh operator process. CONFIRMED by code; not executed. Rehydrate-from-log tests exist (test_rehydrate_outcome.py, test_revenue_restart.py::test_restart_then_owner_completes_the_rehydrated_mission, test_event_log_reproducibility.py), and none covers a crash in the dispatch window. P0, see C3. Fix: on rehydrate, a node with NodeDispatched and no terminal event becomes UNKNOWN (reconcile), not auto re-run.

**Retries.** The mission runtime has no retry or backoff policy. `node.attempts` is incremented (executor.py:run) but never compared to a limit. Any exception fails the mission (runtime.py:792). Retry therefore only happens as an application re-run or a Today re-approve. For `post_json`, there is only a timeout (util.py, 60s default, operators.py:36) and no retry. P2 (fail-closed is safe), but it makes UNKNOWN essential.

**Timeouts.** HTTP clients have a 60s timeout (operators.py). The `bridge.HTTPOperatorClient` transport takes no timeout argument at the call site (bridge.py:74). There is no per-node or per-mission timeout in `Executor.run` or `_execute_wave` (the ThreadPool join at runtime.py:~925 waits forever for `as_completed`). A hung handler blocks the mission and holds its run lock. P1. Fix: node deadline, `future.result(timeout)`, then UNKNOWN.

**Concurrency.** Resource-key serialization (mission/concurrency.py) is sound, with conservative over-serialization of unresolved templates. Waves commit results serially in node order (runtime.py:~930). Approval serialized with run via the per-mission lock, and a double approval fails with `ApprovalError "no pending approval"` (runtime.py:1156-1165). Tests: test_mission_concurrency.py, test_concurrent_execution.py. Ok in-process (multi-process: see above).

**Deadlines.** `ExpectedTransition`/`evaluate_transition`/`monitor_transitions` (reliability.py:63-94) are pure and tested (test_reliability_order.py::test_transition_overdue), but are used only in commercial/order.py:61, service.py:96 and receivables.py:92. `Node` has no deadline. `run_batch(deadlines=...)` only orders scheduling (resource_scheduler.py:78). Today `_Case.deadline` is display only (today.py:81, 359). Nothing emits an incident for "deadline near, no verified completion" on a mission or Today item. Gap, scenario 12.

**Cancellation.** `cancel_mission` (runtime.py:371-388) records MissionCancelled and settles compensation if done side-effecting nodes exist. There is no interruption of an in-flight node (the thread pool and the remote call keep running), and no late-result fence for the mission path. The external TaskManager does handle it correctly: cancel is final, and a later provider SUCCEEDED cannot flip the state (agent_gateway/external/lifecycle.py:~108-122, CONFIRMED by reading; the test is in the external gateway suite, not re-run). Mission path P1: a result landing after cancel is `_finish`ed only if the wave join returns, and no check of the cancelled state precedes commit (INFERRED).

**Compensation.** Strong. Evidence-bound (durable log probe), registry-declared undo only, own-approval option, idempotent per `{key}:undo:{attempt}`, COMPENSATION_FAILED terminal attention state, never inferred as rolled back (executor.py:compensate, compensation_runner.py, receipt.py). Tests: test_compensation_authorization.py etc. (passing). One concern: compensation is triggered by failure, including ambiguous failure (see C1), and `proves_execution` is derived from the result dict, which for a timeout is absent. Keep as is, but gate it behind UNKNOWN resolution.

## 4. Independent postcondition check

Not present for external side effects.
- `CompositeVerifier.verify` (mission/verify.py:140-188) checks: result contains the declared outputs, failure-shaped denylist (`FAILED_STATUSES`), declared output values truthy, permissions still granted, and `StateAssertion`s against the result or world. All of it reads the operator's own return value. The docstring says "no consequential operator result becomes authoritative World State until the acceptance policy succeeds", but the check is on the claim.
- `external/verification.py` `verify_result`: binds the result to the request digest, then VERIFIED if `artifact_refs` is non-empty. It never queries the provider or a system of record. CONFIRMED by probe (scenario 10): an adapter returning `artifact_refs=("artifact:made-up",)` produced receipt SUCCEEDED, state `waiting`, `delivery_verified: True`.
- Read-after-write exists in a separate plane: the Obligation engine (integration/sync.py, tests/test_integration_obligation.py, test_integration_phase1_events.py::test_durable_loop_dropped_write_detected_then_replayed_to_satisfaction), which is a genuine independent check, but it is not what gates mission node completion or Today.
- `NodeSucceeded` is appended after the inline verify, with no distinct event for verification start. `VerificationRecorded` is logged (evidence.py:20).

Fix (C2, P0): add a `PostconditionProbe` SPI, `probe(receipt) -> FOUND|ABSENT|ERROR`, called by an independent read path (provider message-status API, ledger lookup, file open and parse). Success requires FOUND. ERROR gives UNKNOWN. For the external adapter, make `ExternalAgentAdapter.verify_effect(ref)` mandatory for side-effecting capabilities and treat `artifact_refs` as claims. `CompositeVerifier` should require an `assertion` sourced from `probe`, not from `result`, for any node with `side_effecting=True`.

## 5. Receipts and tamper evidence

- `mission_receipt` (mission/receipt.py:20-54) folds the event log: steps with full results, approvals (seq, node, capability, decision; no approver, no input hash), outcome, compensation, evidence ref ids. No hash, no signature, no previous-receipt link. It is recomputed from the log on every call.
- Event log: `Event` has seq and ts only. The JSONL, DuckDB and Postgres stores (mission/store.py, event_backends.py:55-134) are append-only by convention. No chain hash, no signing, and no DB trigger or revoked UPDATE/DELETE grant. Anyone with file or DB write can edit history undetectably. CONFIRMED (no hash in store.py/event_backends.py).
- `runtime_contracts.models.execution.ExecutionEnvelope` (content-addressed `binding`, optional detached `signature`) and `ExecutionReceipt` (envelope_binding, idempotency_key, outcome `executed|refused|failed`, `side_effect_digest`, `attestation`, `receipt_id = content_hash`) are the best design: per-receipt tamper-evident via content addressing, bound to the authorizing envelope. Gaps: not hash-chained to each other, `attestation` empty for the local membrane ("empty is honest"), outcome set has no `unknown`, and `mission_receipt` does not use `ExecutionReceipt` (only membrane.py:88,207 and execution_backend.py use it; ExecutionReceipts are not appended to the mission log). `ActionReceipt` (projects/contracts) in external/operator.py has status, external_id, error, decision_id, with no hash binding to the request digest.
- `InterventionRecord` (intervention_record.py:36-60) is a frozen dataclass: ids, selected action, alternatives, evidence refs, policy version, score, proposed/approved/executed timestamps, execution_ref. Append-only JSONL/Postgres, `OutcomeLink` as separate events. No hash, no signature; the Postgres table has no immutability guard (only INSERT in code). Same class of gap.
- Approval tokens are HMAC-signed, bound to operator, capability, mission, node, `inputs_sha256`, exp and a nonce (approval.py:1-25). This is good, but replay protection is per-process (above).

Fix (C5, P1): add `prev_hash` and `entry_hash = sha256(prev_hash || canonical(event))` per mission stream at `append`; periodically sign a checkpoint head (Ed25519, key outside the host) and anchor it externally. Add `unknown` to the `ExecutionReceipt.outcome` set (runtime-contracts change). Make receipts reference `previous_receipt_id`.

## 6. Canonical transcript vs prompt projection, secrets in traces

- The harness design (agent-harness-dev branch `fix/context-correctness-observability`, commit 6578e89, agent_harness/trace.py) is correct in shape: canonical messages recorded at append time, before any manager rewrites them, content-addressed in `blobs/<sha16>.txt`; a per-call projection records hashes and lengths of what was shown versus canonical; full tool results and args are kept. It is opt-in and the recorder never fails the run (`_safe`).
- Problems: (a) no redaction anywhere in `TraceRecorder`; it writes full tool output, tool args, assistant text and the system prompt (`trace.add(0, messages[0])`), so any secret read by a tool or pasted by a user lands in plain files (default umask). `agentic_os/channels.py:32 redact()` and gate_notify's 0600 umask exist in agentic-os but are not applied to this trace. (b) `sha()` truncates to 16 hex chars (64 bits) and `blob()` skips writing if the file exists, so a collision or a pre-planted blob file silently substitutes content; there is no chain or signature on `trace.jsonl`. (c) Archives in context-runtime (`compression/transcript.py:131,210,221` produce `[archived A####: ...]` stubs) are stored in `archive_dir` with no redaction either (not deep-read).
- agentic-os mission path: `NodeSucceeded` stores the full result in the event log and `mission_receipt` returns it. Credentials are broker-mediated and destroyed after use (executor.py:`_provision`/`_release`), results can still echo secrets if an operator returns them. `SecurityMonitor` events are described as "already the redacted boundary facts" (security_monitor.py:128). `MissionTrace` is spans only.
- Fix (C6, P1): run `redact()` plus a secret-pattern and high-entropy scanner on every blob and projection before write; use full SHA-256; create the files 0600 in a 0700 directory; hash-chain `trace.jsonl`; document the trace as sensitive.

## 7. Placeholder-contamination guard (scenario 11)

- Located: /Users/arybach/projects/agent-harness-dev/agent_harness/context.py:26-47 (`_PLACEHOLDER_START`, `_PLACEHOLDER_ANYWHERE`, `placeholder_in`, `placeholder_arg`) and agent_harness/loop.py:151-158. Stub producers: context-runtime/context_runtime/compression/transcript.py:28,131,210,221, plus `ELIDED` and `chars omitted to save context` in context.py:22,87.
- Behaviour: before a tool call runs, every top-level string arg is checked; a hit returns "ERROR: argument 'X' is a context placeholder ... Nothing was run" as the tool result, emits `placeholder_blocked`, and counts a tool error. Also: PinSet keeps task instructions verbatim; archive-id redirect in read_file.
- Regression test: tests/test_context_correctness.py::test_an_action_carrying_a_placeholder_is_refused_not_run (reproduces the observed `write_file` with "[archived A0048: 17339 chars] ..." and asserts only the real write reached the tool), plus parametrized `test_placeholder_text_is_recognised` and `test_real_content_is_not_mistaken_for_a_placeholder`. CONFIRMED: 25 passed (run on a copy in the scratchpad; the dev dir was not touched).
- Gaps (CONFIRMED by running `placeholder_arg`):
  - Not merged: ~/projects/agent-harness (branch feat/ours-h2) has no `placeholder_arg`. The commit exists only on `fix/context-correctness-observability`.
  - Only top-level string args: `{'edits':[{'new':'[archived A0048: 9 chars] x'}]}` returns None. Nested dict/list args pass.
  - `_PLACEHOLDER_START` matches only at the string start (with leading whitespace). `"intro\n[archived A0048: 9 chars] x"` returns None; the case-changed `[Archived A0048` returns None. A model that prefixes real text then imitates a stub bypasses it.
  - Guard sits only in `run_loop`. Nothing equivalent in agentic-os (a model or tool result containing a stub could be written to a connector, e.g. via `personal_agent.connected_app_action` or an outreach send).
- Fix (P1): merge to main; scan args recursively (strings in dict/list); match the stub anywhere on a line start, case-insensitive; add the same check at the governed-operator boundary (reject text bound for an external send that matches the stub patterns); add tests for nested, mid-string and case variants.

## 8. Stale evidence, duplicate submissions, artifact format

- Stale evidence. Observations carry `known_at` and a provenance quality, and UNKNOWN fails closed for A0 evaluation (observation.py:28-53, observation_store.py:24,124, historical_replay.py:39-48). That protects retrospective learning. For execution: the Today digest binds recipients, content and attempt, and a changed digest returns 409 `stale_approval` (today.py:474-478; tests `test_wrong_or_stale_digest_is_refused_and_sends_nothing`, `test_mutated_content_invalidates_the_reviewed_digest`). The mission runtime's `ApprovalGranted` has no inputs hash and no expiry; the approval is looked up by node id and merged with `edit` (runtime.py:989-1007), and the HMAC token is minted over inputs resolved at dispatch (operators.py:67-69), so world state that changed between approval and execution is signed as approved. `GateExpired` is a notice-only marker (store.py:328-332). INFERRED, P1. Fix: record `inputs_sha256` and `world_epoch` in `ApprovalGranted`; at dispatch compare and re-park on mismatch; add an expiry that moves the gate to expired (blocked), not just a notice.
- Duplicate submissions. Today: per-item `phase in (sent, outcome)` replay returns the stored receipt (today.py:462-465), test_approve_is_idempotent_no_duplicate_send passes (CONFIRMED). Mission: parked-node check plus run lock (above). Webhook: `EventInbox` (integration/events.py:73-97) validates the HMAC and dedupes on (provider, event_id), tests/test_integration_phase1_events.py::test_inbox_validates_signature_appends_and_dedupes passes. EventInbox has no production caller outside tests and `_seen` is in memory only (CONFIRMED by grep); Telegram builds `interaction_id=tg:{chat}:{message_id}` (channels_telegram.py:122) but no dedupe store is shown. P1 for scenario 5: a duplicate webhook delivered after restart, or to a second replica, is processed twice.
- Artifact format. No check that a produced file matches the requested type (mime, extension, magic bytes, parseable). Only `projects/contracts.py:62` has a `mime_type` field (display) and tests/test_integrations_file_formats.py tests the file-provider read/write, not delivery verification. Scenario 13 is a gap (see below).

## 9. Scenario matrix

| # | Scenario | Existing test? | Status |
|---|----------|----------------|--------|
| 5 | duplicate webhook/retry => at most one side effect | Partially. tests/test_operator_sdk.py::test_operator_invoke_dedupes_on_idempotency_key, test_mission.py::test_executor_idempotency_dedupe, test_today.py::test_approve_is_idempotent_no_duplicate_send, test_integration_phase1_events.py::test_inbox_validates_signature_appends_and_dedupes, test_revenue_restart.py::test_drain_outbox_is_idempotent. All pass (CONFIRMED). All are single-process, no restart, no cache eviction. | Partial. Gap: P0/P1 across restart/replica. |
| 6 | ambiguous success => UNKNOWN + reconcile before retry | None. Closest: test_today.py::test_connector_failure_is_not_done_and_retry_needs_a_fresh_digest (clean FAILED, not ambiguous). Probe shows stuck item, no UNKNOWN. | GAP, P0 |
| 10 | model claims sent but provider has no record | None. The fake adapter has `false_success` (no artifact), covered by external verification tests; fabricated artifact ref passes (probe). | GAP, P0 for real adapters |
| 11 | placeholder imitation | Yes in agent-harness-dev: tests/test_context_correctness.py (25 pass). None in agentic-os; unmerged. | Covered on branch only; bypass cases untested |
| 12 | deadline approaching with no verified completion | reliability.py unit test only (test_reliability_order.py::test_transition_overdue); not wired to mission/Today. | GAP, P1 |
| 13 | wrong deliverable format | None. | GAP, P1 |

### Test sketches

- S5 restart: build `Operator` with a counting handler; invoke key K; construct a new `Operator` (fresh `_seen`); invoke K again. Assert the handler count stays 1 when a durable idempotency store is configured. Today it is 2 (expected failure, documents C3). Add a Postgres-backed store variant behind a marker. Second test: write NodeDispatched to a JsonlEventStore, "crash", rehydrate, `run()`; assert the node is UNKNOWN/parked, not re-invoked.
- S6: `Today(adapter=TimeoutAfterCommit())` (see scratchpad/probe_c.py). Assert after the exception: item.state == "unknown", `approve` again raises 409 `reconcile_required`, `reconcile(item_id)` queries the adapter `lookup(idempotency_key)`; FOUND => sent/verified, ABSENT => failed with retry allowed; provider effects stay at 1 throughout. Mission variant: operator raising TimeoutError after side effect => NodeState UNKNOWN, no saga call, no MissionState.FAILED.
- S10: adapter returning `artifact_refs=("artifact:made-up",)` with no provider record; `Today.approve` must yield HELD/UNKNOWN, `delivery_verified False`, once a `verify_effect` probe returns ABSENT.
- S11: tests for `placeholder_arg` with nested args, mid-string stubs, `[Archived ...]`; an agentic-os operator test that an outbound send body matching the stub patterns is refused before dispatch.
- S12: mission with a node `deadline_at`; clock past `deadline_at - lead` with no VerificationRecorded => `ReliabilityIncident(OVERDUE)` event and an AttentionKind.AT_RISK item; past deadline => BLOCKED, not silently RUNNING. Same for a Today case with `deadline` set (today.py:81).
- S13: capability declares `output_format="xlsx"`; the operator returns a file that is CSV. Verifier must REJECT on magic bytes (zip + `[Content_Types].xml`) and parse. Parametrize xlsx, docx, pdf, csv, json with renamed-extension files.

## 10. What is working well (keep)

Fail-closed staging (`is_staged` + `staged_not_executed`, runtime.py:808-823); verifier denylist that never mints success from failure-shaped results (verify.py:21-30,100-130); single-use, request-bound approvals in Today and the external bridge (approval_bridge.py:107-120); compensation authorization; credential redemption only at the boundary with destroy and revoke; per-mission run lock; Today's separation of sent versus delivery_verified versus outcome_observed, and its stale-digest refusal.

## 11. Prioritised fix list

P0: C1 UNKNOWN state and reconcile-before-retry (mission, Today, receipts); C2 independent postcondition probe for side-effecting capabilities; C3 durable idempotency and a crash-window rule (NodeDispatched with no terminal => UNKNOWN), plus a cross-process mission lease.
P1: C4 retry/timeout policy tied to UNKNOWN; C5 hash-chained, signed event log and receipts; C6 trace redaction, full hashes, 0600; C7 merge and harden the placeholder guard, add a boundary check in agentic-os; C8 wire ExpectedTransition into mission nodes and Today; C9 artifact-format verification; C10 approval bound to inputs hash, world epoch and expiry; durable webhook dedupe with a production caller.
P2: expose per-node attempts/limits; cancellation fence for late results on the mission path; add a VERIFYING node state event for observability.
