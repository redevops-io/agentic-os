# Changelog

No curated changelog is kept. History:

- Tags: <https://github.com/redevops-io/agentic-os/tags> (`v0.1.0`, `v0.2.0..v0.2.4`, `v0.3.0..v0.3.34`; never deleted or rewritten).
- GitHub releases: <https://github.com/redevops-io/agentic-os/releases> (latest release object `v0.3.30`, tags are ahead).
- Release procedure and known drift between `pyproject.toml`, tags and release objects: [docs/RELEASING.md](docs/RELEASING.md).
- Downstream pins: `mission-sdk` `COMPATIBILITY.md`.

## Unreleased

- Fork reconciliation parity: a fork branch whose call may have been sent but whose outcome is unknown now follows the plan-node contract (a `ReconciliationRequired` record, the reservation stays UNKNOWN, the fork parks the mission in `WAITING_RECONCILIATION` and returns `awaiting_reconciliation`, nothing is resent, a failing join waits instead of compensating around it) and is resolved through `reconcile_node` / the supervisor, after which the same fork continues. A restarted fork reads each in-flight branch's reservation: never-sent branches are sent, side-effecting dispatched ones park, read-only ones re-run, and a success recorded before the ledger settled is settled from the log. Tested by a crash-and-restart of three branches (succeeded / unknown / running) in the production profile.
- Reconciliation supervisor: structured probe results with provider qualifications (a negative is accepted only from a qualified adapter, past its visibility window; a 404 is never one), a durable bounded retry scheduler (backoff, Retry-After, restart-safe budgets, compare-and-swap claims, exactly-once escalation with a stable notification key), mission ownership leases with fencing enforced inside the append, independent branches continuing while one node awaits reconciliation, and wait-state classification so a waiting mission is never treated as stalled. No provider adapters ship yet. See `docs/RECONCILIATION_PROBES.md`.
- Unknown outcomes park instead of failing (`WAITING_RECONCILIATION`): a dispatched node whose outcome is unknown parks the mission durably (survives restarts; `run()` is a no-op while parked), `POST /missions/{id}/reconcile` resolves it with evidence (approver/admin credential only, actor from the credential), and the SAME mission resumes without rerunning completed work. At-most-once dispatch from #553 is unchanged. Fork branches keep their own semantics; provider probes, periodic reconciliation and notifications are the supervisor's.
- Hard limits and reconciliation: the serial mission loop enforces `deadline`, `wall_clock_s`, `usd` and `tokens` (`HardLimitReached`); reservations carry a durable record (mission, node, attempt, idempotency key, provider reference); `mission/reconcile.py` settles or releases a reservation only when an independent probe proves the outcome (otherwise it stays counted and is flagged), with a manual resolution CLI; the runtime refuses to dispatch a reservation that was already dispatched, in every profile. No provider probes ship yet.
- Network egress boundary (F10): `deploy/egress/` (allowlist forward proxy with an SSRF guard and a JSON decision log, inbound forwarder, self-test), optional `deploy/apps/compose.egress.yml` overlay that puts the apps stack on an internal network behind the proxy, `deploy/k8s/networkpolicy-egress.yaml`, and `--require-egress` / `egress_proxy_configured` in the production preflight. Opt-in; the default install is unchanged. See `deploy/egress/README.md` for guarantees and limits.
- Route authorization (F20/F21): one enforcement layer (`agentic_os/app_kit/route_authz.py`) and a policy for every write route of the 17 apps (human / agent / service contexts, access classes, production refuses unauthorised and unclassified writes before the handler runs); `approval_required` enforced on every operator `/invoke` in every profile (`AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE=1` is a non-production testing override); CI gates for unclassified routes and unreviewed entry points; kernel unauthenticated write routes inventoried and pinned (F22: the Projects API is a demo surface and must not face a real runtime unauthenticated).
- Operator authority boundary (F19): in the production profile an operator's `/invoke` refuses a side-effecting call that has neither a human approval nor an execution authorization issued after the envelope and policy were evaluated; authorizations are signed, single-use, bound to the exact call and replay-protected across replicas; mission identity comes from signed claims; direct-action flow `POST /actions/authorize`; `AGENTIC_OS_PROFILE` diagnostics in `/health`, operator `/_security` and `python -m agentic_os.mission.profile --require-production`. Known gaps: apps' own REST routes (F20). See `docs/SECURITY_PROFILE.md`.
- Authority as an invariant: `AGENTIC_OS_PROFILE=production|development|test` (production requires a durable guard in `MissionRuntime`, `AgentGateway`, `Fleet` and checks every node against the owner's envelope); serial nodes and fork branches share one gate; reservation lifecycle RESERVED/DISPATCHED/SETTLED/RELEASED/UNKNOWN with atomic, idempotent transitions; Fleet agent work and background jobs are inside the envelope. Known gap: operator `/invoke` direct calls (audit F19).
- Standing grants: `PolicyRule.approval_mode = standing_grant_satisfies` lets an explicit `mission-authority/v1` grant stand in for an ordinary approval gate (never DENY, never a mandatory approval, never beyond its limits); durable `SqliteEnvelopeStore`; `AGENTIC_OS_REQUIRE_AUTHORITY` / `AGENTIC_OS_AUTHORITY_DB`. Existing policy digests unchanged.
- `mission-authority/v1` (`agentic_os/mission/authority.py`) and `AgentGateway(authority=...)`: durable-envelope-style authority with resource/limit/expiry/revocation/attenuation, fail-closed at the gateway, delegated missions inherit the owner's constraints. See `audit/AUTONOMY_AUTHORITY_AUDIT.md`.

## v0.3.43 (security and execution-correctness release)

Merged in #510-#521 (audit reports in `audit/`):
- Live publishes need an authenticated, authorised approver (tenant, channel and grant scope); the actor comes from the credential.
- An ambiguous send is UNKNOWN and reconciled (authoritative lookup or an authorised manual statement) before any retry; provider success claims need an independent probe on real adapters; the idempotency key now reaches the provider.
- Gateway approvals are single-use, expiring, atomic and cannot be self-approved; mission approvals are bound to the inputs the approver saw.
- Durable replay guard (`AGENTIC_OS_REPLAY_DB`; `AGENTIC_OS_REQUIRE_DURABLE_REPLAY=1` refuses to start without it).
- Per-mission hash-chained event log (JSONL and in-memory stores only), assurance sensors, optional approval TTL (`AGENTIC_OS_APPROVAL_TTL_S`).
- KeePassXC secret store; LLM key and provider resolved per call through the broker, changeable at runtime with test and rollback.
- Honest dunning status, Guide wording, fiscal-year close warning; third-party images in `deploy/apps` pinned by digest.
- Production settings to enable: `AGENTIC_OS_REPLAY_DB` + `AGENTIC_OS_REQUIRE_DURABLE_REPLAY=1`, tenant `content.publish` grants, `AGENTIC_OS_APPROVAL_TTL_S`.

## Unreleased

- Docs: README first screen, ARCHITECTURE ownership table, GLOSSARY, STATUS, evidence cards, docs check.
