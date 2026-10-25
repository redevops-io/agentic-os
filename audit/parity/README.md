# Parity: behaviours a port of the Python mission kernel must match (2026-10-23)

The Python kernel (`agentic_os/mission`) is the reference implementation. This note lists the behaviours merged in #510-#521 (+ #542/#543)
that any other implementation of the kernel must reproduce, and the hazards in doing so across languages. Ports of the kernel are
developed and reviewed outside this repository; it holds the reference code and the shared vectors only.

| Behaviour | What the reference does |
|---|---|
| B1 approval bound to the inputs the approver saw | park records an inputs digest and an action summary; the grant records the digest; dispatch recomputes it and invalidates the approval on a mismatch |
| B2 HMAC approval token | bound to operator, capability, mission, node, inputs digest, expiry and nonce; single-use (replay guard) |
| B3 durable replay guard | persistent store for the single-use nonces; production fails closed without it |
| B4 per-mission hash chain | every event carries `prev`/`hash`; `verify_chain` and a receipt head |
| B5 approval TTL | a parked gate stops being approvable after `AGENTIC_OS_APPROVAL_TTL_S` |
| B6 assurance sensors | policy-tightening and other sensors that invalidate approvals |
| B7 idempotency key reaches the handler | the key travels in the body and header and to the handler, with server-side de-duplication |
| B8 ambiguous outcome is not a clean failure | a timeout or 5xx after send is not treated as a clean node failure that runs compensation (issue #526) |
| B9 event-store backends | which backends chain, and how |
| B10 secret stores / broker / live provider change | refs-only manifests, a secret store and broker |
| B11 publish route scope | tenant, channel and grant scoping on publish routes |
| B12 gateway inbox | single-use, expiry and no self-approval |
| B13 UNKNOWN outcome + reconcile | an explicit unknown state and a reconcile step |

## Cross-language hazards (settle with shared vectors BEFORE a port implements the chain or digests)
1. **Canonical JSON.** The Python chain hashes `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` (raw UTF-8). A
   canonicaliser written for the approval token uses the `ensure_ascii=True` path (non-ASCII escaped) and must not be reused for the event
   chain. Other hazards: key order for astral characters, `\b`/`\f` escapes, double formatting, and int-versus-float typing (`1` vs `1.0`,
   `1e+20`).
2. **Reading Python-written logs.** A port must preserve `prev`/`hash` on load, and tolerate events written before chaining.
3. **Stale forks of the store.** A fork of `mission/store.py` that produces events without `hash`/`prev` is tolerated by `verify_chain`
   (#543), but should be removed rather than maintained.
4. **Unsafe default (B8, #526).** Any client error, including timeouts and 5xx after send, being a clean node failure that runs
   compensation is unsafe.

## Vectors
`vectors.out` and `gen_chain_vector.py`: a Python-generated 3-event chain (and per-behaviour vectors for approval tokens) to seed shared
conformance fixtures.
