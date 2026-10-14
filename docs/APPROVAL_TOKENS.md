# Signed approval tokens (HTTP operators)

**Problem.** The runtime marks a resumed, human-approved node with `_approval` so a gated handler executes
instead of re-staging. Over HTTP, `Operator.router()` `/invoke` must strip every client-supplied `_approval*`
key (forgery defence, #398), which also stripped the legitimate marker: missions went green with no side effect.

**Design.** `HTTPOperatorClient` removes the marker from `inputs`, mints an HMAC-signed token and sends it as the
top-level body field `approval_token`. `/invoke` strips all `_approval*` keys (case-insensitive, any leading
underscores, recursive through dicts/lists), then verifies the token and injects `inputs["_approval"]` only
from a valid one. Invalid, expired, replayed or mismatched tokens are ignored (handler stages; never a 500).
In-process (`LocalOperatorClient`) is unchanged: the marker travels in inputs, no token.

**Token.** `b64url(payload) . b64url(HMAC-SHA256(secret, payload))`; payload is canonical JSON binding operator,
capability, mission_id, node_id, `inputs_sha256`, the marker, `exp`, random `nonce`. The inputs hash excludes
`_approval*`, `_mission`, `_goal`, `_node` (top level), so an approval for INV-1 $10 cannot be reused for
INV-2 or $999. Comparison is constant-time.

**Config.** (Auto-generated and stored per platform: see [SECRETS.md](SECRETS.md).) `AGENTIC_OS_APPROVAL_SECRET` (read at call time) must be set to the same value on the runtime
**and** on every operator container. Unset/empty: minting returns None, verification always fails; approved
actions stay staged (fail closed) and the client logs one WARNING. TTL defaults to 300 s.

**Replay.** Each operator keeps a bounded in-memory `ReplayGuard` of used nonces (until expiry); a captured
token cannot be reused within its TTL. It is per-process: multi-replica operators need a shared store
(e.g. Redis `SET nonce NX EX`) behind `ReplayGuard.consume` (`Operator.replay_guard`). A full guard fails closed.

**Idempotency.** `Operator._seen` is keyed on (capability, canonical inputs, key); `_approval` is part of the
inputs, so an approved retry is not served a cached staged result. Identical retries still dedupe.

**Not covered.**
- Per-handler gating: apps must still stage unless `_approval` (no generic block of `approval_required`).
- `/agent/run` and other app endpoints that bypass `/invoke` (handled in apps).
- Unauthenticated read capabilities on `/invoke`/`/capabilities`; this adds no caller authentication.
- Secret rotation, and a leaked secret (it can mint any approval); a token is not bound to a caller identity.

## Mission / node binding semantics (fail closed)

The signed payload carries `mission_id` and `node_id`. The verifier decides strictness from the **signed payload**,
never from what the caller sends:

- A dimension that is **non-null in the payload is bound**. The `/invoke` body must present a non-empty value
  equal to it (constant-time compare, normalised to `str`). A missing, `null`, empty or whitespace-only value is a
  mismatch, so the call stays staged (`pending_approval`). Omitting `mission_id`/`node_id` can no longer skip the check.
- A dimension that is **null in the payload is unbound** (the minter had no mission/node, e.g. a direct operator
  call outside a mission). Any presented value, or none, is accepted for that dimension. Blank ids are normalised to
  null at mint time. An unbound token is valid for any mission/node and is only as safe as its other bindings
  (operator, capability, input hash, 300s expiry, single use); a verifier cannot distinguish "attacker omitted"
  from "legitimately unbound", which is exactly why binding is read from the signed payload.
- Only the `mission_id` / `node_id` fields of the JSON body are consulted. `inputs._mission`, `inputs._node` and
  HTTP headers are never trusted for binding.
- Backward compatible: tokens minted by 0.3.29 already carry these fields, so they verify unchanged until expiry.
  No new payload field is required. For approved nodes the runtime sets `_mission` and `_node`, and `HTTPOperatorClient` sends both as `mission_id` / `node_id` (`_node` is set only when an approval exists). Node inputs can never override the reserved runtime keys `_mission`, `_node`, `_goal` (ignored with a warning), and the client refuses to mint a token when an approval is present but `_mission` is missing or blank (the call stays staged), so the runtime cannot mint an unbound-mission token by accident. Non-string ids are coerced to `str`.
- Replay protection and idempotency are unchanged.

## Known limitation: per-process replay/idempotency state

`ReplayGuard` (single-use nonces) and the Operator idempotency cache are in-memory and **per process**. With more
than one replica a replayed token or retried idempotency key can land on another replica and be accepted or
re-executed. Run a **single replica** per operator (or sticky-route by operator) until a shared store implementing
`approval.SharedStore` is wired in; set `AGENTIC_OS_REPLICAS` so a warning is logged if that is violated.

## Saga compensation (rollback) and gated undo capabilities

An undo capability can itself be approval-gated (e.g. `unblock_ip`, `cancel_subscription`, `destroy_delta`).
`Executor.compensate` therefore invokes the undo with a runtime `_approval` marker (`{"approved": True,
"compensation": True}`) plus `_node`/`_mission`: an undo is part of the scope of the action it reverses, and
before handlers were gated these undos ran unconditionally. Over HTTP the client mints the signed token from
that marker like any other approval, so it needs the same `AGENTIC_OS_APPROVAL_SECRET`. Without the secret the
undo stages (fails closed) and the `NodeCompensationFailed` event carries the staged result.

Compensation reporting (formerly a known gap, now closed): `MissionRuntime._saga` records `NodeCompensated`
only when the undo actually succeeded. An undo that staged (`pending_approval` / `executed: False`), errored,
or raised is recorded as `NodeCompensationFailed` (with a `reason` and the raw result), the mission's
`outcome` carries `compensation_incomplete: true` plus `compensation_remaining` (the nodes whose side effects
remain), and `explain()` surfaces both. The saga stays best-effort and never raises past its caller.

### Compensation authorization

`Executor.compensate` is fail-closed. The compensation marker `{"approved": True, "compensation": True}` is
minted only when ALL hold:

1. **The forward step demonstrably executed in THIS mission**: non-blank mission id, matching node id, executed
   capability and idempotency key; the logged `NodeSucceeded` result (or ledgered result) is not staged, not
   failure-shaped, and not `executed: False`. Evidence is the durable event log through the `evidence_probe`
   the runtime wires, so a restart (new executor, same log) still compensates. A staged preview that an
   approval marker let reach `NodeSucceeded` does not count.
2. **The undo is the registered one**: `node.undo` equals the capability registry's declared undo for the
   forward capability (`undo_registry`, wired by the runtime). A planner-attached undo is refused with
   `undo_not_registered`; with no registry wired no marker is ever minted.
3. Undo inputs (`result`) come from the logged/ledgered forward result, never from plan node fields.

Otherwise no operator call is made and `{"compensation_skipped": True, "undo_error": "<reason>"}` is returned
(`forward_not_executed` / `undo_not_registered`); the saga records `NodeCompensationFailed` and
`compensation_incomplete`. Under these conditions a forward step admitted by policy without a human gate
(charge -> approval-gated refund) is rolled back with the marker; a human `ApprovalGranted` is recorded
(`approved`) but is not required. `ApprovalInvalidated` does not block undoing a step that did execute; it never
authorises undoing one that did not. Double compensation is idempotent (also across restarts via
`NodeCompensated`). Client-supplied `_approval*` is never consulted.

Limits operators/humans must know:
- Multi-replica runtimes, or a crash between the undo call and `NodeCompensated` being persisted, can call the
  undo twice: operators MUST honour the stable `{idempotency_key}:undo` key.
- A forward success that crashes before `NodeSucceeded` is persisted gets NO undo and NO marker (fail-closed);
  the effect is left behind and must be reconciled by an operator/human.
- The in-memory ledger is a best-effort fallback for executors without a probe, bounded (LRU 4096): after
  eviction (or a process restart) a completed step is NOT undone (fail-closed: `compensation_skipped` +
  `NodeCompensationFailed` / `compensation_incomplete`). The probe path uses the durable log and has no such limit.
- "Executed" is judged by the strict `verify.proves_execution`: empty `{}`, truthy `staged`, status in
  pending_approval / awaiting_approval / awaiting_connector / staged / held / pending / queued (exact matches; other `awaiting_*` strings are not rejected), `executed: False`, failure shapes, or any of
  these nested under `result` / `results` / `items` all mean NOT executed (the commit-path classifiers are unchanged).
- Production wiring: `MissionRuntime._executor_for` wires the probe and registry for every executor the runtime
  uses. A custom executor that bypasses it has no registry, so its undo runs WITHOUT the marker (a gated undo
  stages visibly).
- One executor serves one runtime; binding a second runtime's event log raises.

The Go port (`go/mission`) does not mirror this: its `Compensate` never mints an approval marker (it sends only
`undo_of`/`result`), so there is no unconditional-approval path to guard.
