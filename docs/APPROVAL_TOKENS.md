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
