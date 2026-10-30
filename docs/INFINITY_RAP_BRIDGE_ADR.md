# ADR: optional Infinity RAP bridge

Status: proposed (first milestone, P1). Scope: [integrations/infinity](../integrations/infinity/README.md). Checks dated 2026-10-29 against `main` at `36b519a` (kernel tag v0.4.1 plus the Pipecat voice milestone).

## Purpose

Let the Infinity agent runtime call ReDevOps actions while the agent hibernates between tool calls, and keep every action under the Governed Agent Gateway. Infinity decides when an agent runs. ReDevOps decides what it may do: authority, budgets, approvals, idempotency and receipts. The native ReDevOps execution is unchanged and is the default; the bridge is an additional entry that a deployment chooses to run.

## Method and sources

| Source | What was taken from it |
|---|---|
| `hydro-project/infinity` at `1abc6fcff1f186124f924b56aa92c3a3684c90c5` (2026-10-27), docs `docs/docs/rap/spec/**`, `crates/rap-protocol`, `crates/rap-client`, `crates/infinity-agent-core`, `crates/infinity-rap-bridge` | the RAP wire format, lifecycle rules and the reference runtime behaviour |
| crates.io metadata for `infinity-agent-core`, `infinity-provider-protocol`, `infinity-rap-bridge`, `rap-client`, `rap-protocol` (all `0.2.0`, published 2026-10-22, Apache-2.0) | versions, licence, checksums (recorded in `harness/Cargo.lock`) |
| the kernel at the commit above | the extension points listed below |

The five crates are identical between the tag `infinity-agent-core-v0.2.0` (`97c2f06`) and `1abc6fc`: the six later commits change documentation, CI and an AWS SDK feature flag only. The RAP specification declares no version number; it is identified here by the repository commit and by `rap-protocol` 0.2.0.

## Decision

1. **A separate distribution.** `integrations/infinity/` with its own `pyproject.toml`, `uv.lock` and import package `redevops_infinity`, as `integrations/voice-pipecat` does. The root `pyproject.toml`, root `uv.lock` and `agentic_os/` are unchanged; `tests/test_infinity_boundary.py` proves the base imports none of it. The package depends on `agentic-os>=0.4.1` and the standard library.
2. **The bridge is a RAP tool server.** In RAP the runtime calls tools; here the runtime is Infinity and the tool server is this package. Discovery, `/invoke`, `/cancel_tool_call` and `/close_thread` are served; nothing else is.
3. **One door into the effect.** Every accepted invocation becomes a `GatewayRequest` and goes through `AgentGateway.invoke`. The bridge grants nothing and has no path to an admin or management API: an operation exists only if the owner declares it and binds it to a registered capability.
4. **No new abstractions where the kernel has them.** Child authority is `ChildSpawner`; approvals are the approvals inbox; unknown outcomes are the authority ledger's lease states and `gateway.reconcile_call`; audit is `EventStore`. Infinity's scheduling, hibernation and model calls are not reimplemented.

## Where it plugs in

| Area | Kernel path | Used as |
|---|---|---|
| Agent Gateway | `agent_gateway/gateway.py` `AgentGateway.invoke`, `reconcile_call` | the single execution path; `protocol="rap"` |
| Registry / manifests | `registry.py`, `contracts.py` | operations bind to capabilities; side effects and approval policy come from the manifest |
| Authority | `mission/authority.py` `AuthorityGuard.check/authorize`, `SqliteLedger` leases | limits, revocation, and the recovery of a crash from lease state |
| Child agents | `mission/children.py` `ChildSpawner`, `ChildCredentialVerifier` | Infinity child threads |
| Approvals | `agent_gateway/approvals.py` `InboxApprovalStore` | a person approves in the inbox; no RAP message can |
| Idempotency | gateway idempotency store; fingerprint binding; revalidation before a replay | the key is the action id |
| Journal | `mission/store.py` `EventStore` (hash chain) | audit of every transition |

## Wire mapping

| RAP | Bridge |
|---|---|
| `GET /.well-known/rap-toolset` | the toolset: declared operations only, JSON-schema inputs, annotation `x-redevops-side-effecting` |
| `POST /invoke` | authenticate, validate, record durably, answer 200; execute later |
| `tool_result` | one per invocation, JSON text with status, reason code, action id, downstream receipt (completed only), `retry_safe`, fixed guidance; `Error: ` prefix for non-success |
| `subscription_event` / `final` | `watch_action` streams status changes of an action; the last event is final; none after cancel |
| `POST /cancel_tool_call` | cancels work that has not been dispatched; a dispatched write is never abandoned; a finished action is not changed; always 200 |
| `POST /close_thread` | cancels the thread's pending work and ends its authority (and its descendants') |
| `thread_ancestors` | the lineage handed to `ChildSpawner` |
| `user_id` | stored as a hash for audit; never used for a decision |
| `user_choice`, `oauth`, `view_update` | never emitted; `user_choice` is a prompt to the runtime's user and is not an approval |

## Security model

- **Authentication, both directions.** `RAP-HMAC-SHA256-V1` signs the method, path, audience, key id, timestamp, nonce and body hash. The key id selects a peer that fixes tenant, principal, scopes, allowed operations and callback origins; a body cannot name them. Unknown fields are refused. Replays (nonce), skew, wrong audience, wrong path and missing scope are refused; every request is authenticated before routing, so an unauthenticated request learns nothing about which paths exist.
- **Callbacks.** Each result carries a signature that also binds tenant, principal, original operation, thread, tool-call id, expiry and an event id that is stable across retries. `CallbackVerifier` is the reference receiver: it rejects a replayed nonce, a changed body, another tenant, an expired message, and acknowledges (without applying) a duplicate event id.
- **Callback URL.** Untrusted input. The origin must equal an allowlist entry for the peer; credentials, fragments, odd numeric hosts, and over-long URLs are refused; HTTPS is required except plain HTTP to a loopback address literal an entry allows; every resolved address must be public unless the entry allows private; the socket connects to the checked address; redirects are not followed.
- **Rate limits and bounds.** Per source address before authentication, per peer and endpoint after it; 64 KiB body cap; chunked bodies refused; in-flight cap per peer.

## Fault model

Order of one action: record (SQLite, committed) → `200` → claim `dispatching` (compare-and-set) → audit record → gateway (lease, handler, lease settled) → decision and queued result (one transaction) → signed POST → mark delivered.

| Crash point | Recovery |
|---|---|
| after record, before the 200 | the runtime asks again; same id and arguments is a duplicate (200); a different body is a conflict (409) |
| before the effect | lease absent or reserved: the action is dispatched again, once |
| after the effect, before the decision | lease settled with the provider's reference: completed with that receipt |
| inside the handler / lease dispatched or unknown | `outcome_unknown`; never sent again; a person reconciles with evidence |
| after the POST, before marking delivered | the result is delivered again with the same event id; the receiver drops the duplicate |
| restart while waiting for approval | the in-memory inbox is empty; the action parks again; nothing runs until a person approves |

A write completes only with a downstream receipt. A model that asks again under a new tool-call id gets the stored receipt, not a second effect (the action id derives from tenant, operation and intent arguments; a deliberate repeat differs in `request_ref`).

## Child threads

A root thread acts under the peer's standing envelope `{tenant}:{principal}`. A child thread is spawned from its parent's envelope: grants only for operations marked `child_allowed` that a parent grant covers, narrowed to that capability with a call limit; a smaller budget; a lifetime clamped to the parent's remaining time; a credential that stays inside the bridge. A recorded lineage cannot be changed. Without a spawner, children are refused. A child may not delegate unless the owner allows it.

## Tests

`tests/` (no network beyond loopback, synthetic data): wire contracts, a four-endpoint negative matrix, callback policy and verifier, idempotency and tenant isolation, authority and approval, crash boundaries (hard-kill injection at each write boundary, including the repository's `CrashingStore`), outcome-unknown and reconciliation, cancellation, deadlines, subscriptions, lineage. `scripts/prove_tests_bite.py` removes each protection in a scratch copy and requires its tests to fail. `tests/test_infinity_sidecar.py` runs the real Infinity runtime (`harness/`, `infinity-agent-core` 0.2.0, scripted model) against the bridge: a governed write, a child thread with the real `thread_ancestors`, a withheld operation, and attacks on the runtime's callback receiver.

## Limits (what was and was not tested)

- One bridge process owns the outbox. The approvals inbox and the gateway idempotency store are process-local; the bridge answers repeats from its own durable record. Multi-process operation is not implemented.
- The model in the harness is scripted. No LLM, AWS, Bedrock, Lambda, DSQL, CDK, TLS termination, or real CRM was used. Plain HTTP on loopback was tested; HTTPS callback delivery was not.
- The stock Infinity local clients send no credentials and its callback listener has no authentication. The harness replaces both with a signing `HttpClient` and a verifying receiver; the Infinity Code daemon and the Lambda runtime were not run.
- Limit and load behaviour was not measured.
- `AgentLifecycleExecutor` (`lifecycle_backend.py`) is a declared contract with no implementation.
