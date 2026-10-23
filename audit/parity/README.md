# Parity review: Go, Kotlin, enterprise Go and .NET vs the Python kernel (2026-10-23)

Read-only review of the behaviours merged in #510-#521 (+ #542/#543). **Nothing was executed in Go, Kotlin or .NET** (no toolchains installed): verdicts are from reading source, with file:line evidence in the per-target reports below. Python is the reference.

| Target | What it is |
|---|---|
| `agentic-os/go/mission` | hand port of the Python mission runtime |
| `redevops-runtime-kotlin` @5d74e17 | JVM/Spark port of context, mission and discovery runtimes; its `mission-runtime/kernel` is a *partial* mission port (no run loop that parks nodes) |
| `agentic-os-enterprise/go` | diverged hand port (mission + canonical/identity/controlauth/events/grpc); ~10 mission files differ from the kernel's Go port |
| `agentic-os-enterprise/dotnet` | 94-line wire-record codec + 2 tests, not a runtime |
| enterprise Python | subclasses the kernel runtime but carries a **stale fork** of `mission/store.py` |

| Behaviour | Go (kernel) | Kotlin | Enterprise Go | .NET |
|---|---|---|---|---|
| B1 approval bound to inputs the approver saw (park digest + summary, granted digest, dispatch recheck) | ABSENT | ABSENT | not reviewed in detail (diverged fork) | n/a |
| B2 HMAC approval token | PRESENT (byte-identical, shared vectors) | PRESENT (byte-identical, ApprovalGateParityTest) | PRESENT (identical, shared vectors) | n/a |
| B2 single-use replay | PRESENT in-process | PRESENT in-process | in-process | n/a |
| B3 durable replay guard + production fail-closed | ABSENT | ABSENT | ABSENT | n/a |
| B4 per-mission hash chain + verify + receipt head | ABSENT | ABSENT (existing `HashChainLedger` uses a different formula, no payload) | ABSENT | ABSENT |
| B5 approval TTL | ABSENT (compensation approvals only, 3600 s) | ABSENT (compensation only, 3600 s) | n/r | n/a |
| B6 assurance sensors | ABSENT | ABSENT | n/r | n/a |
| B7 idempotency key reaches the handler | PARTIAL (body+header, server dedupe; no handler context) | PARTIAL (key passed to handlers) | n/r | n/a |
| B8 ambiguous outcome not treated as clean failure | ABSENT (timeout/5xx => NodeFailed + saga) | ABSENT (any exception => saga) | ABSENT | n/a |
| B9 event-store backends | JSONL + memory, neither chains | in-memory only | n/r | n/a |
| B10 secret stores / broker / live provider change | n/r | ABSENT (read-only `.env` reader only) | PARTIAL (refs-only manifest) | PARTIAL (consumes kernel Vault store) |
| B11 publish route scope (tenant/channel/grant) | n/a | n/a | PARTIAL (role+tenant SoD; no channel/grant) | n/a |
| B12 gateway inbox single-use/expiry/no-self-approval | n/a | n/a | PARTIAL (single-use nonce; no inbox) | n/a |
| B13 UNKNOWN + reconcile | n/a | n/a | ABSENT | ABSENT |

## Cross-language risks (must be settled with shared vectors BEFORE any port implements the chain or digests)
1. **Canonical JSON.** The Python chain hashes `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` (raw UTF-8). Go's existing `canonicalJSON` escapes non-ASCII (ensure_ascii=True semantics) and Kotlin's `Json.canonical` likewise; .NET additionally differs on `\b`/`\f` escapes, key order for astral characters and double formatting. Int-vs-float typing (`1` vs `1.0`, `1e+20`) is a second hazard. The approval-token canonicaliser (ensure_ascii=True path) already has vectors and matches; **the event-chain canonicaliser is a different function** and must not reuse it.
2. **Reading Python-written logs.** A port must preserve `prev`/`hash` on load (Go drops them today).
3. **Stale enterprise fork.** `agentic_os_enterprise/mission/store.py` produces events without `hash`/`prev`; fixed on the kernel side by #543 (`verify_chain` tolerates them), but the fork itself should be reconciled with the kernel store.
4. **Unsafe default shared by all ports (B8, issue #526).** Any client error, including timeouts and 5xx after send, is a clean node failure and runs compensation.

## Vectors
`vectors.out` / `gen_chain_vector.py`: a Python-generated 3-event chain (and the per-report vectors for approval tokens) to seed shared conformance fixtures. `redevops-conformance` currently has no fixtures for approval tokens or the mission chain.

## Reports
`go_kernel.md`, `kotlin.md`, `enterprise_go_dotnet.md` (the last file's summary table is misaligned; this README is the authoritative summary).
