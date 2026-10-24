# Agent security, reliability and business-execution audit: summary

Date 2026-10-23. Scope: the audit plan in `redevops-agent-security-reliability-audit-implementation-plan-2026-10-23`.
Method: four read-only audit workstreams (A architecture/threats, B identity/authority/policy, C execution integrity,
D apps/business catalog), each with file:line evidence labelled CONFIRMED / INFERRED / UNTESTED. Existing offline tests
were run (about 500 pass; the 13 failures present on `main` are environmental: macOS `preexec_fn` sandbox tests, a
duckdb sink test, two projects_api tests). Reports:
[01-architecture-and-threats.md](01-architecture-and-threats.md) (architecture map, data flow, threat model),
[02-authority-and-execution.md](02-authority-and-execution.md) (identity/authority/policy, execution integrity, side-effect paths),
[03-apps-and-business.md](03-apps-and-business.md) (app boundaries, business-admin catalog).
The living remediation log (findings F1-F22 and every implemented step) is [autonomy-authority-audit.md](autonomy-authority-audit.md).
Pinned by tests/scripts, so not merged: [operator_capability_inventory.md](operator_capability_inventory.md),
[kernel_unauthenticated_routes.json](kernel_unauthenticated_routes.json); cross-language parity notes are in [parity/](parity/).

## What the audit found (headline)
| ID | Finding | Sev | Status after this work |
|---|---|---|---|
| A1 | Publish routes (`/api/missions/{id}/decide`, content approve) post to real accounts with no authentication; actor taken from the body | P0 | **FIXED** `fix/publish-routes-require-approver` |
| C1 | No UNKNOWN outcome; timeout after a possible commit => FAILED/stuck, retry gets a fresh key and can double-send | P0 | **FIXED (Today / external operator)** `fix/unknown-outcome-reconcile`; mission path (NodeState UNKNOWN + saga rule) still open |
| C2 | Verification not independent: provider's own artifact refs yield SUCCEEDED | P0 for real adapters | **FIXED (probe SPI + require_independent)**; real adapters must supply a probe |
| (new) | Handler never received the idempotency key: the provider request carried an empty key | P0 | **FIXED** (same branch) |
| C3/F10 | Replay/idempotency state per process | P0/P2 | **PARTLY FIXED** `fix/durable-replay-guard` (approval nonces durable). Operator result cache, EventInbox, TaskManager and the crash-window rule (NodeDispatched without a result => UNKNOWN, not re-dispatch) are still open |
| F1/C10 | Approval not bound to the inputs the human saw | P1 | **FIXED** `fix/mission-approval-binds-inputs` |
| F2 | InboxApprovalStore: replayable, no expiry, self-approval | P1 | **FIXED** `fix/inbox-approval-single-use` |
| F6 | Human gates never expire | P1 | **FIXED (opt-in AGENTIC_OS_APPROVAL_TTL_S)** `feat/mission-assurance-and-gate-ttl` |
| C5 | Event log/receipts not tamper-evident (docs claimed a hash chain) | P1 | **FIXED for JSONL + memory stores** `feat/hash-chained-mission-log`; DuckDB/Postgres backends and signed/anchored heads still open |
| D (HIGH) | `chase_overdue` always "done"; Guide over-claims; "month-end" close is a fiscal-year lock; ERPNext cases assumed delivery | HIGH | **FIXED** `fix/false-success-and-overclaims` |
| D | Edge Sentinel has no agent-assurance sensors | P1 | **PARTLY**: read-only sensor catalog `mission/assurance.py`; wiring into Edge Sentinel's case spine open |
| B/key-audit | LLM key only from env, no broker; no KeePassXC; no live change | owner req. | **DONE** `feat/llm-credentials-via-broker` |

## Still open (not implemented here)
F3 per-person approver credentials and SoD on by default; F4 payload-derived policy facts (amount/destination);
F5 agent_id/run_id/delegation chain; F7 MCP `readOnlyHint` trust and stdio env inheritance; F8 `make_authorizer`
default-deny; C4 retry/timeout policy; C6 trace redaction/full hashes/0600; C7 placeholder guard merge and
agentic-os boundary check (agent-harness is a separate repo with long tests running); C8 deadlines on mission
nodes; C9 artifact-format verification; the invoice and tax-obligation state machines and obligation registry;
tax/e-invoicing provider abstraction; open-core defaults (`_pre_exec_ok` always True, egress allow-all,
`GovernedEnvelope` presence flag, `safe_url.py` has no callers); other unauthenticated routes listed in
`02-authority-and-execution.md` (admin-console, `/m/*/api` proxy, `interaction.reply`, chat channels).

## Branches (all local, none pushed)
Independent unless noted; `feat/mission-assurance-and-gate-ttl` builds on `feat/hash-chained-mission-log`.
