# Revision-aware approvals

A reusable governance capability for every rAIls App. A human approval authorizes **one immutable revision of one artifact for one action, exactly once**.
It is not specific to any app: an app opts in per capability with two declarative fields.

Code: `agentic_os/mission/revisions.py` (the ledger), `agentic_os/mission/revision_gate.py` (runtime glue).
Tests: `tests/test_revision_ledger.py`, `tests/test_revision_aware_approvals.py`, and the consumers `apps/social-autopilot/test_revision_aware_publish.py`, `apps/outreach-engine/test_revision_aware_approve.py`.

## Why

The mission runtime already binds an approval to the node's *inputs digest* and (optionally) a signed token (`approval.py`). Both are about the inputs of one
action. Neither knows about an artifact that **changes over time** while a decision about it is pending (a draft post, a pitch, a document):

| situation | before | now |
|---|---|---|
| draft edited while the approval is pending | the old request stays open; it can still be approved | the request is `SUPERSEDED` in the same transaction as the new revision, the waiting mission is cancelled and the log says why |
| draft edited after the human clicked approve, before the action ran | the approval still applies to whatever is there | `consume` refuses (`superseded`); nothing executes |
| the same approval presented twice, or two workers racing | one in-memory marker; a restart or second worker forgets it | `GRANTED -> CONSUMED` is a compare-and-swap in a durable store; exactly one wins |
| "who approved what, and what happened to the other requests" | only ApprovalGranted/Rejected on one mission | one hash-chained, append-only history per artifact, across missions |

## Using it

```python
from agentic_os.mission.operator_sdk import capability

# producer: success registers a new revision of the artifact (supersedes approvals of older ones)
capability("social.draft", handler, revision_produces={"artifact": "social/post/{post_id}", "digest": ["copy", "media"], "target": "{post_id}"})

# consumer: runs only under a GRANTED, current, matching approval for exactly this revision; always human-gated
capability("social.publish", handler, revision_bound={"artifact": "social/post/{id}", "digest": "head", "target": "{id}"})

rt = MissionRuntime(registry, executor, revision_ledger=RevisionLedger("/var/lib/agentic-os/revisions.db"))   # or AGENTIC_OS_REVISION_LEDGER=<path>
```

`revision_bound` / `revision_produces` are plain dicts so they survive the `/capabilities` JSON. Templates are `str.format` over the node's resolved inputs
(and, for a producer, the result). A missing or empty field refuses (an artifact key like `social/post/None` is never bound).

`digest` says where the revision's content digest comes from:

| value | meaning |
|---|---|
| `"head"` (consumer) | approve whatever the head revision is when the gate parks; the ledger guarantees it is still the head when the action runs |
| `"inputs"` (consumer) | the artifact *is* the action's inputs; each distinct input set is a revision, no producer needed |
| `"<field>"` / `["f1","f2"]` | digest of those input fields (consumer; the revision must already exist and be the head) or result/input fields (producer) |
| `"result"` (producer) | digest of the whole result |

**Opt-in is explicit and fails closed.** A revision-bound capability on a runtime with no ledger does not run; it fails with `RevisionApprovalRefused: no_ledger`.
The two bundled consumers (social-autopilot, outreach-engine) declare the metadata only when `AGENTIC_OS_REVISION_LEDGER` is set (or `revision_aware=True`),
so deployments that do not configure a ledger are unchanged.

## Lifecycle

```
register_revision(N) -- supersedes REQUESTED (and by default GRANTED) approvals of revisions < N, same transaction
request_approval(rev=head)  ->  REQUESTED --grant--> GRANTED --consume (CAS)--> CONSUMED      (final)
                                    |  \--reject--> REJECTED   (final)
                                    \--> EXPIRED (ttl)  /  SUPERSEDED (new revision)           (final)
```

* An approval binds `(artifact_key, rev, digest, action, target, tenant, principal)`. Requesting for a non-head revision is refused.
* `grant` is refused (and recorded) if the approval was superseded, expired, already decided, or its revision is no longer the head. A human who was
  reviewing revision 1 when revision 2 appeared approves nothing.
* `consume` is the execution gate. One `BEGIN IMMEDIATE` transaction checks: approval exists, tenant, binding (artifact/action/target/rev/digest), not already
  consumed, status `GRANTED`, not expired, revision still the head; then `UPDATE ... WHERE status='GRANTED'` and requires `rowcount == 1`. A refusal is written as an event
  in the same transaction and the caller must not execute.
* The runtime calls it at dispatch (`_revision_admit`, a second admission seam next to `_pre_exec_ok`, called on the serial and concurrent paths, tested, and on the fork-branch path, wired but not covered by a fork-specific test) **before**
  `NodeDispatched`, so "still valid" and "about to execute" are one step.
* `RevisionPolicy(supersede_granted=True)` (default). Set `False` only where executing the exact revision a human approved is acceptable after the artifact
  moved on; `REQUESTED` approvals are still superseded. `forbid_self_approval=True` refuses an approver equal to the requester.
* Re-parking the same gate re-uses its open approval instead of opening a second one.

## Mission-log events (additive)

`NodeParked.revision` and `ApprovalGranted.revision` carry `{artifact_key, rev, digest, target, approval_id, action}`. New events:
`ApprovalSuperseded`, `ApprovalConsumed`, `RevisionApprovalRefused {code, detail}`, `RevisionRegistered`, `RevisionRegistrationFailed`.
A superseded gate is reconciled into the waiting mission as `ApprovalSuperseded` + `ApprovalInvalidated` + `NodeSkipped`, then the mission is cancelled (it was about to act on
content that no longer exists; the app starts a new mission for the new revision). Reconciliation runs after every revision this runtime registers, at the start of every `run`,
and on `MissionRuntime.reconcile_revisions()`. Missions owned by another runtime/process are left for that runtime.

## Ledger audit record

Tables: `artifacts`, `revisions` (immutable), `approvals` (state), `events` (append-only). Event types: `revision_created`, `requested`, `granted`, `rejected`, `superseded`,
`consumed`, `expired`, `replay`, `revision_conflict`, `rejected_stale`, `rejected_<code>`. Each event stores `prev_hash` and `hash = sha256(prev_hash || canonical_json(body))`.
`RevisionLedger.history(artifact_key)`, `.verify_chain()` and `.export(artifact_key)` (revisions, every approval in every state, every event, chain verdict) are the audit surface.
Refusal codes: `stale_revision`, `superseded`, `replay`, `not_granted`, `not_pending`, `expired`, `binding_mismatch`, `tenant_mismatch`, `unknown_approval`,
`unknown_artifact`, `digest_mismatch`, `target_not_in_revision`, `revision_conflict`, `self_approval`, plus gate codes `no_ledger`, `no_revision_approval`, `binding_unresolvable`.

## Threat model

Assets: the integrity of "a human approved *this*". Adversaries: a stale or confused client, a racing worker, a compromised producer/agent that edits content after approval, a replaying
caller, another tenant, and an actor with write access to the mission log or the ledger file.

| threat | control | residual |
|---|---|---|
| approve content, then change it | revision N+1 supersedes approvals of N in the same transaction; `consume` requires head | an out-of-band store with no revision registration (see below) |
| approve-vs-edit race | both serialize on the ledger's write lock; `consume` re-checks the head inside its transaction; tested with threads | none within one host |
| replay / double-approve / two workers | CAS `GRANTED->CONSUMED`; `rowcount==1`; tested with threads and 6 OS processes | consumption is at dispatch: a crash after consume and before the operator ran leaves the approval spent (fail-safe: a new approval is needed, nothing executes twice) |
| approval reused for another action/target/artifact | binding fields are compared at consume; mismatch does not burn the approval | |
| cross-tenant use | tenant bound at register/request/consume | tenant comes from `Mission.tenant` (`default` when unset); the identity plane must set it |
| forged `ApprovalGranted` in the mission log | a revision-bound node needs a ledger approval id in the grant, and the ledger must hold it `GRANTED` for the same binding | whoever can write the ledger can grant |
| tampering with history | SQL triggers refuse UPDATE/DELETE on `events` and `revisions`; hash chain detects edits, deletions and reordering | an attacker with file access can drop the triggers and rewrite the *whole* chain; anchor the head hash externally for non-repudiation |
| stale click after supersession | `grant` refuses with `superseded`; runtime raises `ApprovalError` and reconciles the mission | |
| self-approval | optional `forbid_self_approval` | off by default |
| ledger unavailable / misconfigured | revision-bound capability fails closed | availability: no ledger means no publish |
| rollback re-enabling vulnerable behaviour | the ledger file is the only state; deleting it empties history *and* approvals, so nothing previously granted can execute | do not restore an old ledger snapshot over a newer one |

## Limits (stated, not hidden)

* **Single host.** SQLite WAL with `BEGIN IMMEDIATE` is atomic across threads and processes on one machine. Several hosts need the same interface over a shared transactional store
  (Postgres); the SQL is deliberately plain. Do not put the file on a network filesystem.
* **Revisions exist only when something registers them.** Producers (`revision_produces`) and `digest: "inputs"` consumers do. An edit made directly in a third-party UI (e.g. Postiz) does not;
  `social.publish` therefore keeps its pre-existing content-hash check against the live post as a second guard (tested).
* **Fork-branch approvals** are decided by the fork controller, which does not call the ledger; a revision-bound capability inside a fork branch reaches the same admission seam, where it has no ledger approval id and is refused (by design; not covered by a fork-specific test).
* **Retries.** An approval is single use. A node that failed after consume needs a new approval; there is no silent re-use.
* **Development profile.** With `AGENTIC_OS_PROFILE` unset the runtime does not enforce authority (existing, logged warning). Revision approvals do not change that; run `production` for real actions.
* **Approval tokens** (`approval.py`) are unchanged and are not bound to the revision. The ledger is the authority for revision binding; a token only attests an operator approval of the inputs.
* **Approver identity** is whatever the caller passes as `actor`; authentication of the approver is the control plane's job. An empty actor is recorded as `unattributed`.
