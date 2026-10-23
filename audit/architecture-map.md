# Architecture map (workstream A)

Branch `audit/agent-security-reliability-2026-10-23`, 2026-10-23. Labels: CONFIRMED = code read (and/or existing test run), INFERRED = deduced, UNTESTED = no test exercised it. Paths are relative to `/Users/arybach/agentic-os` unless prefixed `~/projects/`.
Targeted existing tests run (all pass, read-only): `tests/test_agent_gateway*.py` (48 pass, 1 skip), `test_approval_token.py`, `test_app_routes_strip_approval.py`, `test_compensation_authorization.py`, `test_agent_gateway_approvals/writes/egress.py` (122 pass).

## 1. Repos and versions
| Repo | Version | Role | Evidence |
|---|---|---|---|
| agentic-os (this) | 0.3.42 | control plane, Mission kernel, Governed Agent Gateway, 17+ reference apps | pyproject.toml:3 |
| runtime-contracts | 0.3.14 (pinned tag v0.3.14) | wire contracts, hashing, credential-broker protocol, TraceContext | pyproject.toml:15; installed in scratchpad venv `runtime_contracts-0.3.14.dist-info` |
| discovery-runtime v0.1.19, knowledge-frontier v0.2.0, redevops-rag v0.4.0 (opt), redevops-connectors v0.1.0 (opt) | pinned git tags | intent discovery, retrieval, SaaS adapters | pyproject.toml:17-46 |
| ~/projects/context-runtime | 0.1.0 (git w4-59-gc255945) | "Core Runtime": context planner + its own control plane + tools | pyproject.toml:3 |
| ~/projects/agent-harness | 0.1.0 (v0.1.0-52-gaac5a43) | tool registry + approval gate + agent loop; STATUS.md says alpha; "no real LLM provider wired in this repo" | STATUS.md:5-18 |
| Go port | go/mission | not audited | go/README.md |
Not in repo/not audited: `reel` (video-ad handlers), redevops-connectors (live adapters), enterprise overlay (authority gate, metering, credential broker, sandbox) -- all are *injected seams* here.

## 2. Entry points (CONFIRMED unless noted)
- CLI: `agentic_os/cli.py` (builds `Router.from_config`, cli.py:34; `secrets get` prints secret values, cli.py:61-71).
- Control plane (FastAPI): `agentic_os/control_plane.py` (routes auth per-route: `require_api_key` fail-closed 503, control_plane.py:36-62; starts chat `Gateway` daemon thread :98).
- Mission API: `agentic_os/mission/api.py:19` `build_router` (router-level `GATE` dependency; tenant scoping `_authorize_mission` :49; approve :123).
- Projects UI/API: `agentic_os/projects_api.py` (auth per-route, many unauthenticated demo/sandbox routes).
- Agent Gateway (external agents, MCP + REST): `agentic_os/agent_gateway/protocols/mcp.py:32,59,83`; mounted in projects_api.py:1392-1413 only if `AGENT_GATEWAY_TOKEN` set, read-only grants.
- Operator `/invoke` for every app: single implementation `agentic_os/mission/operator_sdk.py:253-277` (`authorize_operator_invoke`, control_auth.py:174); mounted by apps/*/app.py and `app_kit/suite_server.py:64`.
- Content / Projects mission servers: `agentic_os/content/app.py:19`, `agentic_os/projects/app.py:70` (unauthenticated).
- Admin console: `deploy/admin-console/app.py:31`.
- context-runtime control plane: `~/projects/context-runtime/context_runtime/control_plane/app.py` (`require_api_key` :30 only on some POST routes; `/m/{name}/api/{path}` proxy :291).

## 3. Agent loops
| Loop | Where | Notes |
|---|---|---|
| Mission run loop (deterministic DAG, not an LLM loop) | `mission/runtime.py:645 _run_locked` | topo waves; per-node policy decision :691-708; park/approve; saga on failure. LLM only in planning (`ModelPlanner`, mission/planner.py:108; falls back to keyword `TemplatePlanner` on any parse error :139) |
| Multi-turn tool-calling agent loop | `~/projects/agent-harness/agent_harness/loop.py:55 run_loop` | limits: max_turns=200, max_seconds=3600 (:55-56); no token/$ cap inside loop (spend lives in `spend_proxy.py`); tools via `ToolRegistry.run` with NO principal passed (loop.py:149) |
| Chat gateway poller | `agentic_os/channels.py:319 Gateway.serve_forever` | polls Telegram/Discord; sender allowlist `_authorized` :297; dispatches to `fleet.dispatch` |
| Fleet dispatch (single LLM call per action) | `agentic_os/fleet.py:91`, `router.py:83` | no tool use; `needs_approval` only records an Approval |

## 4. Tool dispatchers (who can reach an external effect)
1. **AgentGateway.invoke** (`agent_gateway/gateway.py:126`) -- governed path for external agents (DIRECT handlers and MISSION delegation). Only CRM send + projects change are write-capable (`write_capabilities.py:49,62`); the live wiring in projects_api is read-only (CONFIRMED by agent report of projects_api.py:1392).
2. **Mission Executor.run** (`mission/executor.py:228`) -> `OperatorClient.invoke` -> HTTP `Operator /invoke` (mission/operators.py:50-85) -> app handler. Approval carried as HMAC token (mission/approval.py:102-149).
3. **agent_harness ToolRegistry.run** (`tools.py:137`) -- authorizer, then `ApprovalPolicy.decide` (tools.py:73); context-runtime re-exports it (`context_runtime/tools/base.py:1-24`).
4. Direct paths that bypass 1-3: see side-effect-inventory.md.

## 5. MCP servers / connector broker
- Outbound MCP server: gateway bridge `McpGatewayBridge` (mcp.py:32) + optional FastMCP shell (:83). Auth = `TokenVerifier` seam; only shipped verifier is `DevTokenVerifier` (auth.py:43, "NOT FOR PRODUCTION": no expiry, no crypto, in-memory) plus `agent_gateway/oauth.py` (not deeply read -- UNTESTED beyond existing test_agent_gateway_oauth).
- MCP client / tool servers: `~/projects/context-runtime/context_runtime/tools/mcp.py`, `tools/mcp_servers/web_search.py` (not read; INFERRED as the third-party-server trust boundary).
- Connector broker: `connections/manager.py:73-106` (store user API key, verify, revoke); credential broker is an SPI only (`executor.py:79-89 CredentialBrokerSpi`), implementation is enterprise (not in repo). Live adapters come from external `redevops-connectors` via `integrations/live.py:71-106`.

## 6. Scheduler
- Node scheduler: `mission/scheduler.py` `TopoScheduler` (concurrency/per-operator/business-hours/approval batching); runtime `max_concurrency` env `AGENTIC_OS_MISSION_CONCURRENCY` (runtime.py:200).
- Per-mission run lock `_run_lock` (runtime.py:627) serialises run/approve.
- No cron/background job scheduler for missions found in agentic_os (grep for schedule/APScheduler: none besides above) -- INFERRED. Background fleet jobs via thread (fleet.py:111).

## 7. Event store (= audit log)
- `mission/store.py:124 EventStore.append` JSONL, fsync, flock; DuckDB/Postgres backends `mission/event_backends.py`. Events are `{seq, ts, type, mission_id, payload}` with **no hash chain, signature, or prev-hash** (store.py:140-141) -- tamper-evidence absent (CONFIRMED by reading append and grep).
- Gateway audit sink default `InMemoryAuditSink` (gateway.py:87-93) -- lost on restart.
- Approvals/audit for control plane: `context.py` jsonl (rewritten on resolve per side-effect agent report).

## 8. Auth
- Control bearer tokens (admin / tenant / approver) `mission/control_auth.py:21-188`; constant-time compare; fail-closed 503 if unset; separation of duty only if `AGENTIC_OS_APPROVER_TOKENS` set (api.py:127-130).
- Gateway: `GatewayPrincipal` (contracts.py:132) via TokenVerifier; capability visibility = scopes subset AND identity-plane permissions (registry.py:78-84).
- Operator /invoke: admin or tenant bearer; approver refused (control_auth.py:186). One shared bearer held by the runtime (operators.py:77-80); no per-capability authz at the operator (INFERRED).
- Node admission seam `MissionRuntime._pre_exec_ok` default `return True` (runtime.py:746-755): open-core does NO identity/tenancy/budget check at dispatch; enterprise overlay expected to override (CONFIRMED default; overlay UNTESTED/absent).
- `ExecutionContext` single-tenant default (execution_context.py:1-20).

## 9. Secret store
- `secrets/stores.py`: EncryptedFile (AES-GCM, 0600), Keyring, Vault. `secrets/bootstrap.py`: plaintext `KEY=value` file `~/.config/redevops/secrets.env` 0600 atomic (bootstrap.py:80-105); approval HMAC secret read from env or that file (mission/approval.py:37-48).
- Credential broker SPI: grant/redeem/revoke at executor boundary (executor.py:169-226), material destroyed in `finally` (:277-280). Broker implementation not in repo.

## 10. Tracing
- `mission/tracing.py` opt-in `MissionTrace` using runtime_contracts `TraceContext` (lazy import, :19). No exporter in repo. Mission event log is the de-facto trace. Executor `SecurityMonitorSpi.observe` at capability boundary (executor.py:73-76, :281-282).

## 11. Approvals (three independent mechanisms)
1. Mission gate: `PolicyDecisionPlane.decide` (policy_decision.py:101) -> park (runtime.py:1010) -> `approve` (runtime.py:1143-1213) -> `ApprovalGranted` event -> `_approval` marker (runtime.py:989-1008) -> signed token over HTTP (approval.py).
2. Gateway gate: `ApprovalStore` seam, default `_NoApprovals` blocks (gateway.py:68-70); `InboxApprovalStore` (approvals.py:45-88) keyed by `intent_hash`.
3. Harness gate: `ApprovalPolicy` (agent_harness/tools.py:56-85).
Plus app-local Basic-auth approval (`OPERATOR_BASIC_AUTH`) minting `_approval` (apps/*/core.py), and `agentic_os/approval_gate.py:16 gated()` (bare truthiness; not used by any in-package handler per agent report).
