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

**Config.** `AGENTIC_OS_APPROVAL_SECRET` (read at call time) must be set to the same value on the runtime
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
