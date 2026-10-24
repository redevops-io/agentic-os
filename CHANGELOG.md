# Changelog

No curated changelog is kept. History:

- Tags: <https://github.com/redevops-io/agentic-os/tags> (`v0.1.0`, `v0.2.0..v0.2.4`, `v0.3.0..v0.3.34`; never deleted or rewritten).
- GitHub releases: <https://github.com/redevops-io/agentic-os/releases> (latest release object `v0.3.30`, tags are ahead).
- Release procedure and known drift between `pyproject.toml`, tags and release objects: [docs/RELEASING.md](docs/RELEASING.md).
- Downstream pins: `mission-sdk` `COMPATIBILITY.md`.

## Unreleased

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
