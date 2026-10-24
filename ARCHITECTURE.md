# Architecture: who owns what

This page is the single ownership map. It links to the code and docs that own each concern instead of
repeating them. Detail lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); terms are in
[docs/GLOSSARY.md](docs/GLOSSARY.md). `agentic-os` is the integrated control-plane and deployment surface plus the
Mission kernel; it is not a separate fourth runtime.

| Concern | Owner (code) | Owner docs / notes |
|---|---|---|
| **Interfaces** (CLI) | [`agentic_os/cli.py`](agentic_os/cli.py) | `agentic-os` command; [README quickstart](README.md#quickstart) |
| **Interfaces** (HTTP control plane) | [`agentic_os/control_plane.py`](agentic_os/control_plane.py), [`mission/api.py`](agentic_os/mission/api.py) | [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md#control-plane-auth-idempotency-cache-and-multi-replica-limits) |
| **Interfaces** (Projects UI + API) | [`agentic_os/projects_api.py`](agentic_os/projects_api.py) | API + one-origin server owned here. UI **source** is [`redevops-projects`](https://github.com/redevops-io/redevops-projects); `agentic_os/projects_ui/` is git-ignored and fetched at wheel build from the release pinned in [`packaging/projects_ui.lock.json`](packaging/projects_ui.lock.json) ([`scripts/fetch_projects_ui.py`](scripts/fetch_projects_ui.py)) |
| **Interfaces** (external agents) | [`agentic_os/agent_gateway/`](agentic_os/agent_gateway/) | [GOVERNED_AGENT_GATEWAY_IMPLEMENTATION_PLAN.md](GOVERNED_AGENT_GATEWAY_IMPLEMENTATION_PLAN.md) |
| **Interfaces** (developer SDK) | external: [`mission-sdk`](https://github.com/redevops-io/mission-sdk) | depends on this repo |
| **Persistence** (Mission events, source of truth) | [`mission/store.py`](agentic_os/mission/store.py), [`mission/event_backends.py`](agentic_os/mission/event_backends.py) | append-only; in-memory/JSONL, DuckDB, Postgres backends |
| **Persistence** (control-plane context + approvals/audit log) | [`agentic_os/context.py`](agentic_os/context.py) | append-only log |
| **Persistence** (permission grants) | [`agentic_os/permissions.py`](agentic_os/permissions.py) | encrypted at rest; [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md#permissions-plane--access-control-admin) |
| **Persistence** (secrets) | [`agentic_os/secrets/`](agentic_os/secrets/) | [docs/SECRETS.md](docs/SECRETS.md) |
| **Authorization** (data access: row scope, column mask) | [`agentic_os/permissions.py`](agentic_os/permissions.py) | [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md#permissions-plane--access-control-admin) |
| **Authorization** (mutating endpoints) | [`mission/control_auth.py`](agentic_os/mission/control_auth.py) | bearer token, fail closed |
| **Authorization** (human approval gates) | [`mission/approval.py`](agentic_os/mission/approval.py), [`approval_gate.py`](agentic_os/approval_gate.py) | [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md#signed-approval-tokens-http-operators), [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md#gate-notifications) |
| **Authorization** (external-agent calls) | [`agent_gateway/gateway.py`](agentic_os/agent_gateway/gateway.py) | identity, permissions, risk, approval, audit in one path |
| **Model routing** (cost tiers, budget) | [`agentic_os/router.py`](agentic_os/router.py) | the cost-aware router; example config in the README is illustrative |
| **Model routing** (by data classification, fail closed) | [`agentic_os/governance/routing.py`](agentic_os/governance/routing.py) | module present; its wiring into the default router was not audited for this page |
| **Retrieval / context selection** | external: [`context-runtime`](https://github.com/redevops-io/context-runtime) (planning), [`redevops-rag`](https://github.com/redevops-io/redevops-rag) (index and search) | this repo only holds source connectors: [`agentic_os/sources.py`](agentic_os/sources.py) and `sources_*.py` |
| **Intent discovery** | external: [`discovery-runtime`](https://github.com/redevops-io/discovery-runtime) | pinned in [`pyproject.toml`](pyproject.toml) |
| **Wire contracts and hashing** | external: [`runtime-contracts`](https://github.com/redevops-io/runtime-contracts) | conformance state: its `STATUS.md` |
| **Receipts** (mission Action Receipt) | [`mission/receipt.py`](agentic_os/mission/receipt.py) | folded from the persisted event log |
| **Receipts** (inference) | [`governance/receipt.py`](agentic_os/governance/receipt.py) | |
| **Receipts** (email delivery) | [`intelligence/email/`](agentic_os/intelligence/email/) | |
| **Connectors** | external: [`redevops-connectors`](https://github.com/redevops-io/redevops-connectors); contracts in [`agentic_os/integrations/`](agentic_os/integrations/) | |
| **Reference apps** | [`apps/`](apps/README.md) | catalog: [`modules.yaml`](modules.yaml) |
| **Conformance gate (this repo)** | [`tests/conformance/`](tests/conformance/) | [docs/CONFORMANCE.md](docs/CONFORMANCE.md) |

Counts that appear in other READMEs (apps, modules, services) are derived in
[docs/evidence/aos_inventory_counts.yaml](docs/evidence/aos_inventory_counts.yaml).
