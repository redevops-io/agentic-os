# External Agent Gateway (`agent-gateway/v1`)

A provider-neutral governed boundary through which external **personal agents** (Muse / ChatGPT /
Claude / custom) can initiate ReDevOps Missions or act as governed execution nodes — without owning the
Project, evidence, authorization, or learning state. Implements
`REDEVOPS_EXTERNAL_AGENT_GATEWAY_IMPLEMENTATION_PLAN`, extending the existing
`agentic_os.agent_gateway` (agent-gateway/v0) rather than duplicating its governance.

## Phase 0 — what is reused vs net-new

**Reused from `agentic_os.agent_gateway` (NOT reproduced):** the governed pipeline, `CapabilityManifest`
+ registry + visibility filter, RiskTier/ApprovalPolicy/EgressAction/DataClass, OAuth 2.1 + PKCE, MCP +
REST adapters, the egress engine, the inbox-approval store, the undo window, and the inbound
`missions.delegate_goal` path.

**Reused platform contracts (consumed, not replicated):**
| Contract | Import |
|---|---|
| Operator SDK | `agentic_os.mission.operator_sdk` (`capability`, `Operator`) |
| Governance receipt/decision | `agentic_os.projects.contracts` (`Decision`, `ActionReceipt`) |
| Runtime events (v10) | `agentic_os.mission.events` (`RuntimeEvent`, `capability_event`, …) |
| Identity | `agentic_os.overlays` (`Principal`) |
| Mission Runtime | duck-typed public contract (`create_mission`/`run`/`approve`/…) |
| Learn | `discovery_runtime.learn` (strategy-only; guarded by `assert_strategy_only`) |

> There is **no platform `ActionRequest`** (only edge-sentinel's app-local one). The governed path binds
> the existing intent digest + `Decision.decision_id` instead of inventing a parallel `ActionRequest`
> (the plan's "no duplicate Governance abstraction" acceptance criterion).

## Net-new modules (this subpackage)

| Phase | Module | What it adds |
|---|---|---|
| 0 | `../external_agent_capabilities.yaml`, `contracts.load_capability_audit` | UNKNOWN-first provider audit |
| 1 | `contracts.py` | `AgentIdentity/Capabilities/PermissionScope/TaskRequest/Ref/Status/Input/Result`, `TaskState`, `ExternalAgentAdapter` |
| 2 | `inbound.py` | external goal → **real** Mission (requested NL permissions = advisory constraints) |
| 3 | `approval_bridge.py` | trusted approval → `Decision`; prose≠auth; single-use; digest-bound |
| 4 | `operator.py` | `ExternalAgentOperator` — governed node → `ActionReceipt` (verified truth, not provider claim) |
| 5 | `lifecycle.py` | `TaskManager` — idempotent, monotonic, cancel-final, dup-callback-safe, restart-safe |
| 6 | `verification.py` | provider claim ≠ truth; refute false success; **abstain** on uncertainty |
| 7 | `observation.py` | emits **real** `RuntimeEvent` v10 for Edge Sentinel (observer-only) |
| 8 | `fake_adapter.py` | `FakeExternalAgentAdapter` — the only VERIFIED provider; drives every scenario |
| 10 | `evaluation.py` | frozen adversarial corpus (all fail-closed) + `assert_strategy_only` Learn boundary |

Phase 9 (Projects UI + Sidekick surfaces) is a presentation layer over these contracts.

## Invariants (plan §3)
External-agent text is never authorization · approval is identity- and version-bound · external agents
cannot bypass Governance · ReDevOps owns durable context · provider results are evidence, not truth ·
Mission correctness never depends on provider availability · Learn improves strategy only.

### Realized v1 authorization invariant
> **A governed external action is authorized by a `Decision` bound to the exact canonical intent digest;
> the gateway does not define a parallel `ActionRequest` authority.**

The plan's diagram reads `ActionRequest → Governance → Decision → Operator → ActionReceipt`. The platform
has **no** `ActionRequest` type (only edge-sentinel's app-local one), so implementing the diagram
literally would have created a *second* authorization object competing with `Decision`. Instead the
approval bridge binds the request's canonical `intent_digest()` to a projects `Decision.decision_id`
(`approval_bridge.py`), and the `ExternalAgentOperator` stamps that `decision_id` onto the `ActionReceipt`
(`operator.py`). One authorization object, one digest, end to end — mutation changes the digest (approval
void) and a consumed approval cannot be replayed. This is the intended realized shape of the v1 boundary,
not a shortcut around the diagram. The same shape governs the social plane: a `SocialActionRequest` binds
its content digest to a `Decision`, and an edited draft (new digest) voids the approval.

## Tests
`PYTHONPATH=/mnt/backup/projects/discovery-runtime .venv/bin/python -m pytest tests/test_external_agent_gateway.py`
(the core path needs only `.venv`; `discovery_runtime` is used by the social/Learn phases).
