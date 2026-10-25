# Architecture, data flow and threat model (workstream A)

Baseline audit reports, 2026-10-23. Findings and their status: [README.md](README.md).

## Contents

- [Architecture map (workstream A)](#architecture-map-workstream-a) (was `architecture-map.md`)
- [Data flow: one workflow end to end (workstream A)](#data-flow-one-workflow-end-to-end-workstream-a) (was `data-flow.md`)
- [Threat model (workstream A)](#threat-model-workstream-a) (was `threat-model.md`)

## Architecture map (workstream A)

Branch `audit/agent-security-reliability-2026-10-23`, 2026-10-23. Labels: CONFIRMED = code read (and/or existing test run), INFERRED = deduced, UNTESTED = no test exercised it. Paths are relative to `/Users/arybach/agentic-os` unless prefixed `~/projects/`.
Targeted existing tests run (all pass, read-only): `tests/test_agent_gateway*.py` (48 pass, 1 skip), `test_approval_token.py`, `test_app_routes_strip_approval.py`, `test_compensation_authorization.py`, `test_agent_gateway_approvals/writes/egress.py` (122 pass).

### 1. Repos and versions
| Repo | Version | Role | Evidence |
|---|---|---|---|
| agentic-os (this) | 0.3.42 | control plane, Mission kernel, Governed Agent Gateway, 17+ reference apps | pyproject.toml:3 |
| runtime-contracts | 0.3.14 (pinned tag v0.3.14) | wire contracts, hashing, credential-broker protocol, TraceContext | pyproject.toml:15; installed in scratchpad venv `runtime_contracts-0.3.14.dist-info` |
| discovery-runtime v0.1.19, knowledge-frontier v0.2.0, redevops-rag v0.4.0 (opt), redevops-connectors v0.1.0 (opt) | pinned git tags | intent discovery, retrieval, SaaS adapters | pyproject.toml:17-46 |
| ~/projects/context-runtime | 0.1.0 (git w4-59-gc255945) | "Core Runtime": context planner + its own control plane + tools | pyproject.toml:3 |
| ~/projects/agent-harness | 0.1.0 (v0.1.0-52-gaac5a43) | tool registry + approval gate + agent loop; STATUS.md says alpha; "no real LLM provider wired in this repo" | STATUS.md:5-18 |
Not in repo/not audited: `reel` (video-ad handlers), redevops-connectors (live adapters), enterprise overlay (authority gate, metering, credential broker, sandbox) -- all are *injected seams* here.

### 2. Entry points (CONFIRMED unless noted)
- CLI: `agentic_os/cli.py` (builds `Router.from_config`, cli.py:34; `secrets get` prints secret values, cli.py:61-71).
- Control plane (FastAPI): `agentic_os/control_plane.py` (routes auth per-route: `require_api_key` fail-closed 503, control_plane.py:36-62; starts chat `Gateway` daemon thread :98).
- Mission API: `agentic_os/mission/api.py:19` `build_router` (router-level `GATE` dependency; tenant scoping `_authorize_mission` :49; approve :123).
- Projects UI/API: `agentic_os/projects_api.py` (auth per-route, many unauthenticated demo/sandbox routes).
- Agent Gateway (external agents, MCP + REST): `agentic_os/agent_gateway/protocols/mcp.py:32,59,83`; mounted in projects_api.py:1392-1413 only if `AGENT_GATEWAY_TOKEN` set, read-only grants.
- Operator `/invoke` for every app: single implementation `agentic_os/mission/operator_sdk.py:253-277` (`authorize_operator_invoke`, control_auth.py:174); mounted by apps/*/app.py and `app_kit/suite_server.py:64`.
- Content / Projects mission servers: `agentic_os/content/app.py:19`, `agentic_os/projects/app.py:70` (unauthenticated).
- Admin console: `deploy/admin-console/app.py:31`.
- context-runtime control plane: `~/projects/context-runtime/context_runtime/control_plane/app.py` (`require_api_key` :30 only on some POST routes; `/m/{name}/api/{path}` proxy :291).

### 3. Agent loops
| Loop | Where | Notes |
|---|---|---|
| Mission run loop (deterministic DAG, not an LLM loop) | `mission/runtime.py:645 _run_locked` | topo waves; per-node policy decision :691-708; park/approve; saga on failure. LLM only in planning (`ModelPlanner`, mission/planner.py:108; falls back to keyword `TemplatePlanner` on any parse error :139) |
| Multi-turn tool-calling agent loop | `~/projects/agent-harness/agent_harness/loop.py:55 run_loop` | limits: max_turns=200, max_seconds=3600 (:55-56); no token/$ cap inside loop (spend lives in `spend_proxy.py`); tools via `ToolRegistry.run` with NO principal passed (loop.py:149) |
| Chat gateway poller | `agentic_os/channels.py:319 Gateway.serve_forever` | polls Telegram/Discord; sender allowlist `_authorized` :297; dispatches to `fleet.dispatch` |
| Fleet dispatch (single LLM call per action) | `agentic_os/fleet.py:91`, `router.py:83` | no tool use; `needs_approval` only records an Approval |

### 4. Tool dispatchers (who can reach an external effect)
1. **AgentGateway.invoke** (`agent_gateway/gateway.py:126`) -- governed path for external agents (DIRECT handlers and MISSION delegation). Only CRM send + projects change are write-capable (`write_capabilities.py:49,62`); the live wiring in projects_api is read-only (CONFIRMED by agent report of projects_api.py:1392).
2. **Mission Executor.run** (`mission/executor.py:228`) -> `OperatorClient.invoke` -> HTTP `Operator /invoke` (mission/operators.py:50-85) -> app handler. Approval carried as HMAC token (mission/approval.py:102-149).
3. **agent_harness ToolRegistry.run** (`tools.py:137`) -- authorizer, then `ApprovalPolicy.decide` (tools.py:73); context-runtime re-exports it (`context_runtime/tools/base.py:1-24`).
4. Direct paths that bypass 1-3: see side-effect-inventory.md.

### 5. MCP servers / connector broker
- Outbound MCP server: gateway bridge `McpGatewayBridge` (mcp.py:32) + optional FastMCP shell (:83). Auth = `TokenVerifier` seam; only shipped verifier is `DevTokenVerifier` (auth.py:43, "NOT FOR PRODUCTION": no expiry, no crypto, in-memory) plus `agent_gateway/oauth.py` (not deeply read -- UNTESTED beyond existing test_agent_gateway_oauth).
- MCP client / tool servers: `~/projects/context-runtime/context_runtime/tools/mcp.py`, `tools/mcp_servers/web_search.py` (not read; INFERRED as the third-party-server trust boundary).
- Connector broker: `connections/manager.py:73-106` (store user API key, verify, revoke); credential broker is an SPI only (`executor.py:79-89 CredentialBrokerSpi`), implementation is enterprise (not in repo). Live adapters come from external `redevops-connectors` via `integrations/live.py:71-106`.

### 6. Scheduler
- Node scheduler: `mission/scheduler.py` `TopoScheduler` (concurrency/per-operator/business-hours/approval batching); runtime `max_concurrency` env `AGENTIC_OS_MISSION_CONCURRENCY` (runtime.py:200).
- Per-mission run lock `_run_lock` (runtime.py:627) serialises run/approve.
- No cron/background job scheduler for missions found in agentic_os (grep for schedule/APScheduler: none besides above) -- INFERRED. Background fleet jobs via thread (fleet.py:111).

### 7. Event store (= audit log)
- `mission/store.py:124 EventStore.append` JSONL, fsync, flock; DuckDB/Postgres backends `mission/event_backends.py`. Events are `{seq, ts, type, mission_id, payload}` with **no hash chain, signature, or prev-hash** (store.py:140-141) -- tamper-evidence absent (CONFIRMED by reading append and grep).
- Gateway audit sink default `InMemoryAuditSink` (gateway.py:87-93) -- lost on restart.
- Approvals/audit for control plane: `context.py` jsonl (rewritten on resolve per side-effect agent report).

### 8. Auth
- Control bearer tokens (admin / tenant / approver) `mission/control_auth.py:21-188`; constant-time compare; fail-closed 503 if unset; separation of duty only if `AGENTIC_OS_APPROVER_TOKENS` set (api.py:127-130).
- Gateway: `GatewayPrincipal` (contracts.py:132) via TokenVerifier; capability visibility = scopes subset AND identity-plane permissions (registry.py:78-84).
- Operator /invoke: admin or tenant bearer; approver refused (control_auth.py:186). One shared bearer held by the runtime (operators.py:77-80); no per-capability authz at the operator (INFERRED).
- Node admission seam `MissionRuntime._pre_exec_ok` default `return True` (runtime.py:746-755): open-core does NO identity/tenancy/budget check at dispatch; enterprise overlay expected to override (CONFIRMED default; overlay UNTESTED/absent).
- `ExecutionContext` single-tenant default (execution_context.py:1-20).

### 9. Secret store
- `secrets/stores.py`: EncryptedFile (AES-GCM, 0600), Keyring, Vault. `secrets/bootstrap.py`: plaintext `KEY=value` file `~/.config/redevops/secrets.env` 0600 atomic (bootstrap.py:80-105); approval HMAC secret read from env or that file (mission/approval.py:37-48).
- Credential broker SPI: grant/redeem/revoke at executor boundary (executor.py:169-226), material destroyed in `finally` (:277-280). Broker implementation not in repo.

### 10. Tracing
- `mission/tracing.py` opt-in `MissionTrace` using runtime_contracts `TraceContext` (lazy import, :19). No exporter in repo. Mission event log is the de-facto trace. Executor `SecurityMonitorSpi.observe` at capability boundary (executor.py:73-76, :281-282).

### 11. Approvals (three independent mechanisms)
1. Mission gate: `PolicyDecisionPlane.decide` (policy_decision.py:101) -> park (runtime.py:1010) -> `approve` (runtime.py:1143-1213) -> `ApprovalGranted` event -> `_approval` marker (runtime.py:989-1008) -> signed token over HTTP (approval.py).
2. Gateway gate: `ApprovalStore` seam, default `_NoApprovals` blocks (gateway.py:68-70); `InboxApprovalStore` (approvals.py:45-88) keyed by `intent_hash`.
3. Harness gate: `ApprovalPolicy` (agent_harness/tools.py:56-85).
Plus app-local Basic-auth approval (`OPERATOR_BASIC_AUTH`) minting `_approval` (apps/*/core.py), and `agentic_os/approval_gate.py:16 gated()` (bare truthiness; not used by any in-package handler per agent report).

## Data flow: one workflow end to end (workstream A)

Workflow traced: **an authenticated caller asks the Mission API for a customer refund (`billing.refund`, moves money out, Lago credit note)**. This is the highest-consequence path that crosses every stage (trigger, principal, plan, model call, tool proposal, authorization, approval, dispatch, external effect, verifier, receipt). Labels: CONFIRMED = code read; INFERRED = deduced; UNTESTED = no test run covers it. All paths relative to `/Users/arybach/agentic-os`.
Existing tests that exercise parts of this chain and passed on this branch: `tests/test_approval_token.py`, `test_app_routes_strip_approval.py`, `test_compensation_authorization.py` (122 passed together with gateway approvals/writes/egress tests). A single full end-to-end run of this exact refund chain was NOT executed (UNTESTED as one flow; Lago is external).

| # | Stage | Function (file:line) | What happens | Label |
|---|---|---|---|---|
| 1 | Trigger | `build_router.create` `agentic_os/mission/api.py:71` (`POST /missions`, body goal max 20 000 chars :25) | Router-level `GATE` dependency (api.py:24) runs `resolve_caller` (`mission/control_auth.py:150`): admin / tenant / approver bearer, constant-time compare, 503 if no token configured | CONFIRMED |
| 2 | Principal | api.py:73-95 | Tenant token forces its own tenant and server-side `tenant_limits` grants/budget ceiling (control_auth.py:101-127; wildcard refused api.py:84-86). **Admin token default `policy_refs=["*"]` = all permissions** (api.py:93). Principal is only a string id/tenant on the Mission (runtime.py:290-292), not an attested identity | CONFIRMED |
| 3 | Plan | `MissionRuntime.create_mission` runtime.py:278 -> `_plan_and_gate` :438 | `PolicyScopedRegistry(registry, m.policy_refs)` (runtime.py:433) hides capabilities the grants do not cover from planner/compiler. `ActivePlanner.select` picks among candidates; `validate_plan_compensation` (:477); budget projection (:482) | CONFIRMED |
| 4 | Model call | `planner.plan` runtime.py:445 -> `ModelPlanner.plan` `mission/planner.py:124` (or deterministic `TemplatePlanner` :51) | The LLM sees only the goal text and capability hints (planner.py:111-118) and returns outcomes, NOT tools/args; any parse error silently falls back to the keyword planner (planner.py:139-140). Default runtime planner is `TemplatePlanner` (runtime.py:228), i.e. **no model call in the default path**. Where a model is used, routing goes through `GovernedLLM` (`agentic_os/app/llm.py:55`, `governance/routing.py` fail-closed on classification) or the cost router `router.py:83` (cheapest-first, falls back up tiers only on transport errors, budget checked AFTER the call, router.py:95-98). The goal string is interpolated raw into the prompt via `str.replace` (planner.py:127) | CONFIRMED (code); model path wiring in a deployed mission server INFERRED |
| 5 | Tool proposal | compiler binds outcome -> capability (`mission/compiler.py`, `ContextRuntime` CHECK_POLICY intent) | The model never proposes a concrete tool+args; arguments come from `node.inputs` / `$from_world` and launcher-supplied `Mission.inputs` (`_resolve_inputs` runtime.py:950-978). Reserved keys `_goal/_mission/_node` cannot be overridden (runtime.py:955-959, :178) | CONFIRMED |
| 6 | Authorization (policy plane) | `runtime._run_locked` runtime.py:691-708 -> `PolicyDecisionPlane.decide` `mission/policy_decision.py:101` | OR of: mandatory capability flag (:104), `MissionPolicy` rule / `approval:` constraints (:107-111), dynamic risk score >= 0.5 or regulatory or not permissioned (:112). `refund` hits MANDATORY / regulatory markers ("financial", "tax"...) (policy_decision.py:27,207). A DENY rule hard-blocks (`_block_denied` runtime.py:1032). `PolicyEvaluated` event pins policy digest (runtime.py:698-703) | CONFIRMED |
| 7 | Node admission | `MissionRuntime._pre_exec_ok` runtime.py:746 | **Open-core returns True unconditionally**: no identity / tenancy / metering check at dispatch unless an enterprise subclass overrides. Authority-chain check exists only if `Executor.authority` and `authority_for` are both wired (executor.py:231-242) | CONFIRMED default; overlay UNTESTED |
| 8 | Approval (park) | `_park` runtime.py:1010 | Node -> WAITING, `HumanTask` + evidence packet (policy_decision.py:162-185). Packet shows capability, operator, produces, cost, rollback text but **not the concrete resolved inputs (invoice id, amount)** (policy_decision.py:172-185; inputs resolved later at runtime.py:950) | CONFIRMED |
| 9 | Approval (grant) | `POST /missions/{mid}/approve` api.py:123 -> `runtime.approve` runtime.py:1143 -> `_approve_locked` :1149 -> `ApprovalGranted` event :1196-1199 | Tenant check 404 cross-tenant (api.py:49). Approver credential required **only if** `AGENTIC_OS_APPROVER_TOKENS` is set (api.py:127-130); otherwise the requester (or an agent holding the same token) can self-approve. `edit` payload merged into the marker (runtime.py:1005-1007) after `strip_approval_keys` (api.py:134). Grant is bound to node id + capability (runtime.py:1198, `_approved` :1783-1830) | CONFIRMED |
| 10 | Dispatch | `_execute` runtime.py:776 -> `Executor.run` `mission/executor.py:228` | `_resolve_inputs` adds `_approval` marker + `_node` (runtime.py:975-977). Optional authority check, credential broker grant/redeem/revoke (executor.py:243-280), sandbox if capability declares isolation (fails closed without sandbox, :250-258). Ledger of executed nodes for compensation (executor.py:283-289) | CONFIRMED |
| 11 | Transport + signed approval | `HTTPOperatorClient.invoke` `mission/operators.py:50-85` -> `mint_approval_token` `mission/approval.py:102` | Marker is NOT sent in inputs; HMAC-SHA256 token binds operator, capability, mission_id, node_id, sha256 of inputs, marker, exp (300 s), nonce. Refuses to mint without `_mission` (operators.py:62-67). No secret -> no token -> staged (fail closed) | CONFIRMED |
| 12 | Operator authN/Z | `Operator.router().invoke` `mission/operator_sdk.py:253-277` | `authorize_operator_invoke` (admin/tenant bearer; approver refused; tenant mismatch 403). Strips every client `*approval*` key (:270), verifies token incl. `replay_guard` (:274-277), re-injects `_approval` | CONFIRMED |
| 13 | Idempotency | `Operator.invoke` operator_sdk.py:146-183, `_execute` :186-229 | In-memory per-process LRU keyed by (capability, inputs, key); only when a key is supplied; in-flight claim via Event. **Not durable; lost on restart / other replica** | CONFIRMED |
| 14 | Handler gate | `stage_refund` `apps/billing/core.py:486-520` (gate `if not body.get("_approval")` :504) | Bare truthiness of `_approval` (the token was verified only in the router). Missing invoice or ambiguous amount rejected before execution (:488-499) | CONFIRMED |
| 15 | External side effect | `_execute_refund` `apps/billing/core.py:~440-483`; `client.post(.../api/v1/credit_notes)` core.py:477 | Real Lago refund credit note; HTTP >=400 -> `{"executed": False}` | CONFIRMED |
| 16 | Verifier | `_finish` runtime.py:804 -> `CompositeVerifier.verify` `mission/verify.py:173` | `is_staged` w/o marker -> NodeFailed (runtime.py:816-828). Then checks: declared output keys present, failure-shape classifier (`result_failed` verify.py:134), permissions still granted, assertions. **Verifier only inspects the dict returned by the same operator; no independent read-back of Lago** (verify.py:177-217) | CONFIRMED |
| 17 | State commit / event | `world.observe`, `NodeSucceeded` event runtime.py:855-863 | Result dict stored in the event log in full (incl. whatever the operator returned) | CONFIRMED |
| 18 | Receipt | `mission_receipt` `mission/receipt.py:18-47` | Folds `NodeSucceeded` + `ApprovalGranted` + outcome + compensation + evidence refs from the log. **Unsigned**; approvals carry node/capability but not approver identity (`actor` is stored in the event, runtime.py:1197, but not folded into the receipt, receipt.py:35-37) and not the input hash. Underlying log has no hash chain (store.py:140) | CONFIRMED |
| 19 | Failure / undo | `_fail_node` runtime.py:792 -> `_saga` -> `Executor.compensate` executor.py:309 | Undo only if durable evidence shows the forward node executed, undo equals registry-declared undo, own approval if registry requires it, bound to input hash/attempt/exp (executor.py:341-375) | CONFIRMED (tests pass) |

### Differences on the other governed path (external agent via AgentGateway)
`McpGatewayBridge.call_tool` (`agent_gateway/protocols/mcp.py:50`) -> `AgentGateway.invoke` (`gateway.py:126`): idempotency replay check BEFORE visibility/authz (gateway.py:130-136) -> visibility = scopes AND permissions (gateway.py:140-143, registry.py:78) -> `BudgetGuard` (default allow-all, gateway.py:78-80) -> approval decision (gateway.py:153-159; `IF_POLICY` auto-runs when no `policy_allows` injected, :228-231) -> PENDING_APPROVAL for DIRECT side effects (:168-171) -> handler with a `GovernedEnvelope` (:233-238; the envelope is a presence flag, see side-effect-inventory.md) -> egress filter on output (:205-216; default `_AllowAllEgress`) -> audit (`InMemoryAuditSink` default). Approval in `InboxApprovalStore.is_satisfied` (approvals.py:53-64) is keyed on `intent_hash` (contracts.py:164-172) and **never consumed or expired**, so once approved every identical call re-executes (CONFIRMED by reading; the idempotency store only dedupes when the caller supplies a key and is in-memory). The inbox `approve()` method (approvals.py:74-77) performs no approver authorization or separation-of-duty check (authz must be enforced by the caller; UNTESTED who calls it in wiring).

### Key gaps the trace exposes
1. Approver sees action class, not concrete resolved arguments (step 8).
2. Self-approval allowed unless `AGENTIC_OS_APPROVER_TOKENS` configured (step 9).
3. Idempotency, replay guard, gateway audit are per-process (steps 13, gateway).
4. Verifier is self-attesting; receipt unsigned; log not tamper-evident (steps 16-18).
5. Node admission seam is a no-op in open core (step 7).

## Threat model (workstream A)

Scope: agentic-os 0.3.42 + context-runtime 0.1.0 + agent-harness 0.1.0 + runtime-contracts 0.3.14. Status: CONFIRMED = code read and/or an existing targeted test passed; INFERRED = deduced from code/comments; UNTESTED = no test or code path verified. Severity is for this code as shipped (open-core defaults), not for an enterprise overlay that is not in the repo. Refs: `data-flow.md`, `side-effect-inventory.md`, `architecture-map.md` in this directory.

### 1. Trust boundaries
| Boundary | Trusted side | Untrusted side | Enforced where |
|---|---|---|---|
| User -> control plane | operator holding bearer token | anyone reaching HTTP | `mission/control_auth.py:150`; per-route in control_plane.py (not global); many unauthenticated routes (side-effect-inventory B1-B4,B16) |
| Agent (external, via MCP/REST) -> gateway | gateway pipeline | agent + its reasoning ("untrusted but authenticated", contracts.py:7-10) | `gateway.py:126` (tokens: DevTokenVerifier only in repo) |
| Mission runtime -> operator (app) | runtime | operator process | HMAC approval token `mission/approval.py`; bearer shared (operators.py:77-80) |
| Subagent / delegated mission | parent mission grants | delegated goal | `PolicyScopedRegistry` runtime.py:433; `tenant_limits` control_auth.py:101; gateway MISSION kind gateway.py:175-189; `_NullMissionPort` default raises (gateway.py:44-46) |
| Model provider | local boundary | provider endpoint | `governance/routing.py` fail-closed by classification; `app/llm.py:55`; `router.py` has no classification (cost only) |
| Tools / apps | runtime | app handler + its external SaaS | `_approval` truthiness in handler; harness `ApprovalPolicy` |
| Connector / MCP server | broker | third-party API/server | credential broker SPI only (executor.py:79); `redevops-connectors` external; `context_runtime/tools/mcp.py` not read |
| Tenant | tenant A | tenant B | api.py:41-52 (404 cross-tenant), `execution_context.py` single-tenant default |
| Retrieved content | -- | docs/web/email/CRM text | no sanitization layer found; egress redaction only on gateway outputs (egress.py) |
| Admin | token holder | -- | admin token = all tenants, `policy_refs=["*"]` default (api.py:93) |
| Third parties (customers, providers) | -- | inbound webhooks/messages | chat poller allowlist by username (channels.py:297) |

### 2. Threats
Columns: where in THIS code | existing mitigation (file:line) | status | severity.

#### T1 Prompt injection (direct goal / indirect via retrieved or tool text)
- Where: planner prompt built by string replace of goal (`mission/planner.py:127`); LLM output parsed with greedy regex `\{.*\}` (:146) and only fills `outcome/need` strings. Operator results flow into world state and `$from_world` inputs of later nodes (`runtime.py:845-861`, `_resolve_inputs` :962-964). Harness loop feeds tool output straight back to the model (`agent_harness/loop.py:163`).
- Mitigation: model never picks tools/args in the Mission path (planner returns outcomes; binding by compiler/registry) -> blast radius limited to "which outcomes"; PolicyScopedRegistry; MANDATORY/regulatory approval for money actions (policy_decision.py:104,207); parse failure falls back to deterministic planner (planner.py:139). Harness: side-effecting tools gated by ApprovalPolicy (tools.py:73-85).
- Gaps: no content/instruction separation or injection scanning anywhere (grep none); in harness loop the model chooses tool name and args directly; approval gate keyed on tool's self-declared `side_effecting` (default False, tools.py:28).
- Status: CONFIRMED (code) for mitigations and gaps; actual injection success UNTESTED. Severity: Medium (Mission path) / High (harness agents with side-effect tools in `bypass` or `allowlist` mode).

#### T2 Tool-output laundering (untrusted output becomes trusted state)
- Where: `_finish` accepts operator-reported `_observations` with the operator-chosen `confidence` (clamped 0..1), `source`, `source_type` (runtime.py:845-861); `world.observe(node.produces, result, confidence=1.0)` (:843). Downstream nodes consume via `$from_world`; belief fusion only gates on conflict/low confidence (`_belief_issue` runtime.py:1046).
- Mitigation: low-confidence/conflicting beliefs park for disambiguation (runtime.py:762/1084); reserved keys; `strip_approval_keys` on mission inputs (runtime.py:297).
- Gap: an operator (or a result that echoes attacker text, e.g. a CRM comment) can assert `confidence=1.0, source_type="human"`. Egress filter (gateway) regex-redacts only on the way out.
- Status: CONFIRMED (code read). Exploit UNTESTED. Severity: Medium.

#### T3 Malicious delegation (subagent / delegated mission gets more than parent)
- Where: gateway MISSION capability `gateway.py:175-189` passes `arguments` (incl. `constraints`) to `MissionPort.delegate`; REST `POST /missions` accepts caller `constraints` and `policy_refs` (api.py:26-27,71-95).
- Mitigation: tenant callers cannot choose authority: grants from server config, wildcard rejected, budget ceiling (api.py:80-91, control_auth.py:101-127); constraints can only ADD approval gates; approvals never inherit across revision/capability (runtime.py:1783-1830). Authority chain leasing exists as optional Executor seam (executor.py:231-242).
- Gap: admin token default grants `*` (api.py:93); in-process `_pre_exec_ok` no-op (runtime.py:746); gateway `delegate` with a real MissionPort is not wired in repo.
- Status: CONFIRMED mitigations; delegation chain enforcement UNTESTED (enterprise seam absent). Severity: Medium.

#### T4 Model switching after refusal
- Where: `router.py:83-100` falls up tiers only on transport exceptions (URLError/OSError/ValueError); a model *refusal* is a successful text response and is returned as-is (no retry on refusal); `GovernedModelRouter` has no refusal handling either. Agent-harness `llm.py` is a minimal client.
- Mitigation: none specific, none needed for refusal-retry because none exists; but tier fallback to a pricier/less-restricted model on transport failure is silent (no receipt of fallback in router.RouteResult beyond tier name). `InferenceReceipt` (governance/receipt.py) records endpoint when `GovernedLLM` is used.
- Status: INFERRED (no refusal-detection code found by reading router.py/routing.py; harness llm.py not fully read). Severity: Low.

#### T5 Excessive inherited privileges
- Where: admin bearer = all tenants, wildcard grants (api.py:93; control_auth.py:162); the runtime uses the same admin bearer to call every operator (operators.py:77-80) so any operator accepts any capability (no per-capability authz, INFERRED); `PolicyDecisionPlane.permissioned` true when `"*"` in grants (policy_decision.py:80); `_pre_exec_ok` default True; gateway `_AllowAllBudget`/`_AllowAllEgress`/`IF_POLICY` auto-run defaults (gateway.py:57-80,228-231); in-process approvals trusted.
- Mitigation: tenant tokens scoped; approver tokens distinct and cannot invoke (control_auth.py:186); capability visibility = scopes AND permissions (registry.py:78-84); gateway write capabilities approval REQUIRED; tests pass (`test_agent_gateway*.py`).
- Status: CONFIRMED. Severity: **High** (open-core defaults are permissive by design; production relies on overlay).

#### T6 Credential exfiltration
- Where: approval HMAC secret and all platform secrets in plaintext `secrets.env` (secrets/bootstrap.py:80-105) and `agentic-os secrets get` prints values (cli.py:61-71); shell rc edits with unquoted path (bootstrap.py:114,133); Vault URL not sanitised (stores.py:201); token sent to URL taken from provider response (channels_whatsapp.py:121); token in URL for Telegram (channels.py:122); tool outputs fed back to model (harness loop.py:163) could carry secrets; `receipt reason=str(e)` may include URLs (intelligence/email/_base.py:81).
- Mitigation: credential broker redeems only at executor boundary and destroys material in `finally` (executor.py:169-226,277-280), secrets never in `inputs`/log (executor.py:26-28, receipt.py docstring); gateway egress DENY for `SECRET` and regex redaction (egress.py:25-39,101-110); channel errors redacted (channels.py:32,85); file secrets 0600 atomic.
- Gap: broker implementation absent from repo; egress is regex/key-name based (misses e.g. short or novel secret shapes); default egress allows everything (gateway.py:57).
- Status: CONFIRMED for mitigations/gaps; broker behaviour UNTESTED here. Severity: Medium-High.

#### T7 Replay / duplicate actions
- Where: gateway approval not consumed (approvals.py:64) -> identical request re-executes forever after one approval; gateway idempotency store in-memory (gateway.py:101-105) and checked before authz (gateway.py:130-136; key is principal-scoped so low risk); Operator idempotency & `ReplayGuard` per-process (operator_sdk.py:139-142; approval.py:180-204; `SharedStore` protocol has no implementation :152-163, replicas only warn :166-177); nonce consumed before handler runs so a lost response yields staged retry (not duplicate) (approval.py:145); app-level dedupe only Lago `external_id`, consent ledger, Postiz PUBLISHED check; no key supplied -> no dedupe.
- Mitigation: token binds inputs hash/mission/node/exp 300 s/nonce (approval.py:102-149), tests `test_approval_token.py` pass; compensation idempotent (executor.py:344-348); in-flight claim prevents concurrent duplicates in one process.
- Status: CONFIRMED. Severity: **High** for refunds/dunning/publish/`terraform apply` on restart or >1 replica; Medium otherwise.

#### T8 Spoofed receipts / audit tampering
- Where: `mission_receipt` folds an unsigned, un-hash-chained JSONL (receipt.py:18-47; store.py:124-155); anyone with file/DB write can rewrite history; verifier trusts operator-returned dict (verify.py:177-217); gateway `AuditEvent` in-memory; `GovernedEnvelope` self-minted (integrations/execution.py:169); approvals in receipt lack approver identity and input hash (receipt.py:35-37); chat `Context.resolve` rewrites approvals.jsonl.
- Mitigation: append-only API, fsync, flock (store.py:124-150); events pin policy digest/plan fingerprint (runtime.py:698,466); Postgres backend DB-assigned seq (event_backends.py:128); `executor.monitor.observe` at the boundary independent of operator (executor.py:281).
- Status: CONFIRMED. Severity: **High** for financial/legal non-repudiation.

#### T9 Confused deputy
- Where: runtime holds one bearer that every operator trusts (operators.py:77-80); app Basic-auth approver creds mint `_approval` for any input (apps/*/core.py); `intelligence`/`projects` routes act with server credentials for unauthenticated callers (B1-B4, B16); privacy.intake sends company email to caller-chosen address with caller-chosen link host (core.py:270-272, 733); `/m/{name}/api` proxy forwards with server identity (control_plane.py:355).
- Mitigation: tenant pinned onto inputs (`_tenant`, operator_sdk.py:260-266, :280); tenant mismatch 403; approver cannot invoke.
- Status: CONFIRMED. Severity: High (B1/B2), Medium others.

#### T10 Cross-tenant leaks
- Where: mission API 404 cross-tenant (api.py:49-52) and `list_missions` filter (api.py:97); `control_plane /approvals`, `/jobs/{id}` unauthenticated list approval payloads (control_plane.py:502,624); `/missions/{mid}/events` is tenant-checked; execution context single-tenant default (execution_context.py:1-20); in-memory idempotency keys omit tenant in Operator (key = capability, inputs, key; tenant only via `_tenant` input, INFERRED); apps use process-global state (outreach `_STATE["approved"]`).
- Mitigation: tenant tokens, `_authorize_mission`; gateway idempotency key includes tenant/subject (gateway.py:133).
- Status: CONFIRMED for mission API; app-level isolation UNTESTED. Severity: Medium (single-tenant default) / High if multi-tenant deployed on open core.

#### T11 Poisoned retrieval
- Where: retrieval is external (context-runtime, redevops-rag; `sources_rag.py`); retrieved text enters model prompts (harness loop; context-runtime plans) with no provenance-trust levels beyond `derived_from`; world observations take operator-asserted confidence (T2).
- Mitigation: context-runtime design principle "compression keeps derived_from" (ARCHITECTURE.md §1, not code-verified); `VerifiedIntent` sealing pins evidence refs (runtime.py:128-150, epoch pins :464); Postgres source forced read-only (sources_postgres.py:107-125).
- Status: INFERRED / UNTESTED (retrieval code in other repos not read). Severity: Medium.

#### T12 Compromised MCP server / third-party tool server
- Where: inbound: gateway MCP bridge validates bearer only, tool args not schema-validated against `input_schema` (gateway.py invoke never validates; mcp.py:50-56); outbound: context-runtime `tools/mcp.py` and `web_search.py` (not read); connector adapters from external package; Vault/HTTP providers accept any base URL, no SSRF guard (safe_url.py unused).
- Mitigation: gateway returns only `client_view` (contracts.py:215-228), hidden capabilities indistinguishable from unknown (gateway.py:141-143); egress policy on outputs.
- Status: CONFIRMED (gateway side), INFERRED/UNTESTED (outbound MCP client). Severity: Medium-High.

#### T13 Denial of wallet
- Where: harness loop caps only turns (200) and wall clock (3600 s) (loop.py:55-56), no $ cap in loop (spend_proxy separate); `router.py:95-98` checks budget AFTER the paid call, counts output tokens only, in-memory `spent_usd` reset on restart; gateway `_AllowAllBudget` (gateway.py:78); mission `Budget` is projected at plan time (runtime.py:482) and not metered per node in open core (`_meter` no-op runtime.py:770); `POST /dispatch` background jobs unbounded; unauthenticated `intelligence` quote routes call paid providers (intelligence/api.py:65,76); video render usd 0.50 per call ungated (apps/video-ad).
- Mitigation: tenant budget ceiling (api.py:88-91); fail-closed price table in `agent_harness/spend.py` (`SpendError` unpriced model) used by `spend_proxy`; mission concurrency caps.
- Status: CONFIRMED. Severity: Medium-High.

#### T14 Irreversible financial actions
- Where: billing.refund / dunning / subscription cancel (apps/billing/core.py:486-520,306,618), books close/reverse, `infra.provision` terraform apply/destroy; Stripe adapter reachable through `integrations/live.py:71-106` with a presence-only envelope (B5); `revenue/loop.py:73` boolean approval.
- Mitigation (strong, CONFIRMED): approval MANDATORY by regulatory markers (policy_decision.py:27,207); signed single-use token bound to exact inputs; gate precedes execution in core; `refund` requires explicit invoice and amount (core.py:488-499); staged results never commit (runtime.py:816-828); compensation requires evidence + registered undo + own approval (executor.py:309-375); `MissionPolicy` DENY on `irreversible` (policy.py:49-76, test run). Tests passed.
- Gaps: self-approval unless approver tokens set (api.py:127); approver sees no concrete args (policy_decision.py:172-185); replay across restarts (T7); `infra.provision` plan_digest optional.
- Status: CONFIRMED. Severity: **High** (residual).

### 3. Top 5 (by risk)
1. B1/B2 unauthenticated publish/approve on content & projects servers (T9, T14 adjacent) -- Critical/High.
2. Approval/idempotency/replay state is per-process; gateway approval never consumed (T7) -- High.
3. Open-core defaults are permissive: `_pre_exec_ok` no-op, admin `*` grants, allow-all egress/budget, in-process approvals trusted, self-approval default (T5) -- High.
4. Receipts/event log unsigned and unchained; verifier self-attesting (T8) -- High.
5. `GovernedEnvelope` is a presence flag and `safe_url` has zero callers (T6/T12/B5) -- High.

### 4. Not covered / follow-ups
Enterprise overlay (authority gate, broker, sandbox, metering) absent -> all "overlay will tighten" claims UNTESTED. Not read: context-runtime tools/mcp.py, retrieval/store plugins, agent_harness llm/egress_proxy/spend_proxy, runtime-contracts internals, Go port, `agent_gateway/oauth.py` in depth, `reel` package.
