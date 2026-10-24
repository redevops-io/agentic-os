# Identity, authority, execution integrity and side-effect paths (workstreams A-C)

Baseline audit reports, 2026-10-23. Findings and their status: [README.md](README.md). The living remediation log is [autonomy-authority-audit.md](autonomy-authority-audit.md).

## Contents

- [Workstream B: Identity, Authority and Policy Audit](#workstream-b-identity-authority-and-policy-audit) (was `identity-authority-policy.md`)
- [Workstream C: execution integrity audit (read-only)](#workstream-c-execution-integrity-audit-read-only) (was `execution-integrity.md`)
- [Side-effect path inventory (workstream A, item 4)](#side-effect-path-inventory-workstream-a-item-4) (was `side-effect-inventory.md`)

## Workstream B: Identity, Authority and Policy Audit

Branch `audit/agent-security-reliability-2026-10-23`. Read-only audit; the only file written is this one.
Date: 2026-10-23. Paths are relative to `/Users/arybach/agentic-os` unless noted.

Labels: CONFIRMED (read in code, or reproduced with a probe), INFERRED (follows from code, not exercised), UNTESTED (no test covers it).
Severity: P0 (exploitable now, high impact), P1 (real gap on a consequential path), P2 (hardening).

Probe script (not in repo): `/private/tmp/claude-501/-Users-arybach-agentic-os/30e85532-95c8-480d-aadc-1f976c943ab3/scratchpad/probe_b.py`.

Terms used below: there are two authorization stacks.
- Mission stack (`agentic_os/mission/*`): `MissionRuntime`, `PolicyDecisionPlane`, HMAC approval tokens, operator `/invoke`.
- Gateway stack (`agentic_os/agent_gateway/*`): `AgentGateway.invoke` for external agents, plus `InboxApprovalStore`.
- Legacy fleet stack: `fleet.py`, `context.py`, `control_plane.py /approvals`.

### 0. Executive summary

Strong: the mission stack's operator approval is a real signed, scoped, single-use, expiring token bound to (operator, capability, mission, node, sha256 of inputs). The gateway has a single governed pipeline, deny-by-default visibility, and tenant tokens that cannot choose their own authority.

Weak, in priority order:

1. **P1 CONFIRMED. The approver is not shown the payload.** The park packet and HumanTask evidence carry capability name, side_effecting and produces, with no input values or amount (`mission/policy_decision.py:157-183`, `mission/runtime.py:1018-1019`). The approval is recorded per node id plus capability, not per payload (`mission/runtime.py:1194-1206`). The HMAC token binds inputs only at dispatch time, from whatever the inputs are then (`mission/operators.py:67-69`). Anything that changes node inputs between park and resume (`$from_world`, selected-capability mission inputs) is covered by the approval. The "approved $100, executed $1000" tamper is blocked only for the gateway path and the Today path, not the mission path.
2. **P1 CONFIRMED. `InboxApprovalStore` (gateway stack) has no expiry, no single-use, no separation of duties.** Probe: the requester approved its own request (`approve(by="agent1")` has no principal check), the same approved call then ran 3 of 3 times. The $100 to $1000 tamper was correctly refused (the intent hash includes args). `agent_gateway/approvals.py:53-64,74-77`. Not wired in production today (only read-only gateway is mounted, `projects_api.py:1392`), so latent, but it is the exported public default.
3. **P1 CONFIRMED. Separation of duties is off by default.** `AGENTIC_OS_APPROVER_TOKENS` unset means the requester token can approve its own mission (`mission/api.py:127-129`; the test `tests/test_mission_api_default_deny.py:319` documents it). Even when set, approvers are one shared token per tenant, and the actor recorded is `approver:<tenant>` (`mission/api.py:136`), not a person. The runtime never compares approver and requester identity. The fleet `/approvals/{id}/{decision}` path uses the same single API key to request and resolve (`control_plane.py:506-514`).
4. **P1 CONFIRMED. Spend policy cannot see the payload amount.** `max_usd` rules and the monetary risk term use the static `node.cost.usd` (`mission/policy_decision.py:154-157`), never the refund or charge amount in `inputs`. A rule like "approve refunds above $500" cannot be expressed.
5. **P1 INFERRED. No run-level identity chain.** There is no `agent_id`, no `run_id` distinct from mission, and no delegation-parent field. `ExecutionContext` has `delegated_by` and `authority_chain` but the mission runtime never populates them. See (a).
6. **P1 CONFIRMED. Approval does not expire.** `GateExpired` is "notice-only: the gate stays open" (`mission/store.py:328-331`). A mission parked for a week can still be approved on stale evidence.
7. **P1 INFERRED. MCP tool side-effect flag trusts the remote server.** `readOnlyHint=true` from an untrusted MCP server makes the tool non-side-effecting, which skips the approval gate (`context-runtime/context_runtime/tools/mcp.py:194-195`). The stdio client also inherits the full parent environment (`mcp.py:40`).
8. P2: node results are written whole to the event ledger without redaction (`mission/runtime.py:862-863`); the gateway returns raw exception text to the client (`agent_gateway/gateway.py:196`); the default egress is `_AllowAllEgress` (`gateway.py:57-59,114`) and relies on handler-declared data classes.

No P0 found. Nothing here lets an unauthenticated caller execute a side effect on a default-configured deployment.

### (a) Run identity: tenant_id, workflow_id, run_id, agent_id, actor_id, delegation parent

| Field | Where | Status |
|---|---|---|
| tenant | `Mission.tenant`, `MissionCreated.tenant` (`mission/runtime.py:282,318`); `GatewayPrincipal.tenant` (`agent_gateway/contracts.py:145`); `ExecutionContext.owner/execution/resource_tenant` (`execution_context.py:51-53`) | Present. Mission-API tenant is forced from the tenant token (`mission/api.py:79`). |
| workflow_id | `ExecutionContext.workflow_id` only (`execution_context.py:60`); no mission-level field. `Mission.template` is a name | Partial. |
| run_id | `mission.id` is the only run identifier; node id is `mission:revision:outcome`. No per-attempt run id on retries | Partial. |
| agent_id | `GatewayPrincipal.client_id` for external agents (`contracts.py:137`). Mission/runtime nodes have no agent id | Missing for internal runs. |
| actor_id | `Mission.principal` (type `Any`); approval `actor` is `admin` or `role:tenant` (`mission/api.py:136`) | Weak: tenant-level, not per person. |
| delegation parent | `ExecutionContext.delegated_by`, `authority_chain` exist (`execution_context.py:56-57,72`); nothing in `mission/` sets them. `rg "ExecutionContext(" agentic_os/mission` shows no population | Dead field. |

Specific defects:

- CONFIRMED P1: `MissionRuntimeAdapter.delegate` (`agent_gateway/mission_adapter.py:42-50`) passes `principal=` and `inputs=` but not `tenant=` and not `policy_refs=`. A gateway-delegated mission is created with `tenant=""` and `policy_refs=[]`. Consequences: (1) the mission API treats it as untenanted, so only the admin token sees it (`mission/api.py:64-66`); (2) tenant attribution on the audit trail is lost; (3) authority is the empty grant set (fails closed, so safe, but unintentionally).
- INFERRED P2: `MissionCreated.principal` is `getattr(m.principal, "id", m.principal)` (`mission/runtime.py:317`). A `GatewayPrincipal` has no `.id` (it has `.subject`), so the whole dataclass object is stored, not the subject id. The event carries scopes and `client_id` as an object repr; the id the audit wants is not recorded as an id.
- Gateway `AuditEvent` carries client_id, subject, tenant, workspace, mission_id (`contracts.py:232-248`), good, but there is no parent request id, and `InMemoryAuditSink` is the only sink (`gateway.py:87-93`; lost on restart).
- Gateway delegation parent: a gateway call that delegates to a mission carries `mission_id` in the result but the mission does not carry the gateway `request_id` back. Parent link is one-way. INFERRED.

Proposed fix: add `run_id`, `agent_id`, `actor_id`, `parent_run_id`, `request_id` to `MissionCreated` and `PolicyEvaluated`/`ApprovalGranted` payloads. Make `delegate` pass `tenant=principal.tenant` and the principal subject. Populate `ExecutionContext.authority_chain` from the gateway principal then mission then node.

### (b) Where grants originate; can tools be called outside the single dispatcher

Grant origins:
1. Mission stack: `policy_refs` set at `create_mission`. Admin token default is `["*"]` (`mission/api.py:97`). Tenant tokens get `AGENTIC_OS_TENANT_LIMITS` only, wildcard rejected, no entry means no grants (`mission/control_auth.py:tenant_limits`, `mission/api.py:85-96`). CONFIRMED, tested (`tests/test_tenant_limits_and_registry.py`).
2. Gateway: `authorize(principal, permission)` is an injected callback. The default is the process identity plane (`registry.py:41-42`). `DevTokenVerifier` stores grants in memory with no expiry and is marked NOT FOR PRODUCTION (`auth.py:42-70`). The only mounted gateway uses a single static bearer and a hard-coded principal in tenant `demo` with read scopes (`projects_api.py:1348-1390`). CONFIRMED.
3. Data plane: `GrantStore` (`permissions.py:178-260`), AES-GCM encrypted, fail closed when sealed. Grants have no expiry, no tenant field, no `not_before`. Admin write is a shared key (`docs/permissions.md`). The `admin` role bypasses everything (`permissions.py:274-275`).
4. Unauthenticated reads: `GET /api/permissions/grants`, `/authorize` and `/preview` are open per `docs/permissions.md` (information disclosure of the grant map, P2).

Single dispatcher: partly true.
- Gateway: `AgentGateway.invoke` is the only entry; adapters call it (`gateway.py:1-14`). Visibility equals permission, and a hidden tool returns the same `unknown_capability` (`gateway.py:140-143`). Good. Tested: `tests/test_agent_gateway_rest.py:36`, `tests/test_agent_gateway_mcp.py:88`.
- Mission stack: handlers are reachable two other ways. (i) operator `POST /invoke` is a direct call path, gated by bearer token and the `_approval` strip (`mission/operator_sdk.py:259-293`); a side-effecting handler must itself use `gated()` (`approval_gate.py:17-22`). Gating is per handler by convention: nothing enforces it for a handler that forgets `gated()`. The conformance catalog tests (`tests/conformance/`) are the safeguard; I did not audit each of the ~27 operator capabilities for `gated()` or `approval_required`. (ii) `LocalOperatorClient` treats `_approval` in inputs as trusted in-process (`operator_sdk.py:313-324`); anything in-process that can write `_approval` into inputs bypasses the gate. `strip_approval_keys` is applied at mission creation and in `create` paths (`mission/runtime.py:297`) and at API routes (tested `tests/test_app_routes_strip_approval.py`).
- Tool registry (`agent_harness.tools.ToolRegistry.run`, `agent_harness/tools.py:137-161`): the authorizer and approval policy run only when a tool is called through `run()`. `ToolRegistry.get(name).run(args)` is public and skips both. INFERRED P2.
- Fleet stack: `Fleet.dispatch` routes by `m.needs_approval(action)`, which is a module-config check on a model-supplied `action` string (`fleet.py:102`). An action name not listed in the module's approval set runs ungated; approval is by action name, not by tool/resource/amount. P1 INFERRED (legacy path).
- `make_authorizer` fails open for any tool not in `tool_resources`/`app_resources` (`permissions.py:346-349`) and only ever checks the `read` action (`permissions.py:351`), so write and delete grants are never enforced on tool calls. CONFIRMED P1 for any deployment that relies on this seam for writes.

### (c) Structured policy inputs vs model-written prose

Mostly structured. `PolicyRule.match` selects on `capability`, `side_effecting`, `irreversible`, `regulatory`, `over_budget`, `cost_usd` (`mission/policy.py:61-75`). Capability side-effect and reversibility come from the registry spec (`mission/compiler.py:82-86`), not from the model. Operator manifests cannot weaken declared security fields (`tests/test_tenant_limits_and_registry.py:72-136`). The gateway decides on manifest tier/approval policy and principal scopes (`gateway.py:145-171`). The classification router uses `DataClassification` and boundary enums (`governance/routing.py`).

Prose or weak signals that still reach a decision:
- `_regulatory` is a substring match over capability name, operator name and permission strings (`policy_decision.py:_regulatory`). Not model prose, but easy to evade by naming. P2.
- `from_constraints` and `_mission_policy_requires` read `approval:` tokens from the free-text `constraints` list (`policy.py:194-211`). On the gateway path the external agent supplies `constraints` (`gateway.py:181`). They can only add approvals (tighten), so this is safe. A hostile agent cannot loosen by this route. INFERRED safe.
- Gap: no payload-level predicate. The structured context lacks amount, destination (recipient, domain, bank account), resource id and tenant ownership of the target. `NodeContext` has none of them (`policy.py:78-87`). `execution_context.resolve_for_node` does check resource owning tenant (`execution_context.py:150-170`), but it is only invoked for nodes naming a resource, and the divergence overlay is enterprise-only. CONFIRMED P1 (amount), P2 (destination).
- `safety.scan_action` is a regex over prompt text (`safety.py:41-47`), advisory only (badge in the UI). Not a control. Easily bypassed by obfuscation; do not count it as a gate.
- Egress classification uses handler-declared `data_classes` (`gateway.py:204`); a handler that declares INTERNAL for PII is not caught. P2.

Proposed fix: extend `NodeContext` with a `facts: dict` extracted by a per-capability deterministic extractor (amount_minor, currency, destination ids, resource tenant) and let `PolicyRule` express `field: amount_minor, gt: N`. Hash the same facts into the approval packet.

### (d) Revocation and expiry

| Mechanism | Revocation | Expiry | Evidence |
|---|---|---|---|
| Mission grants | `change_policy(revoke=...)` removes refs, appends `ApprovalInvalidated` for still-pending nodes, optional retroactive compensate+fail (`mission/runtime.py:1303-1362`) | none on the mission itself | tested `tests/test_authorization_correctness.py:153`, `tests/test_mission_fork_join.py:587,601`, `tests/test_rehydrate_authority.py:25` |
| In-flight node | not interrupted; effective for not-yet-terminal nodes only | no | by design (docstring `runtime.py:1308-1311`) |
| Approval token | none (single use via nonce) | 300 s TTL (`approval.py:104`) | `tests/test_approval_token.py:44,65` |
| ReplayGuard / idempotency cache | per process, lost on restart; multi-replica warning only (`approval.py:140-150,162`) | TTL = token exp | seam `SharedStore` has no implementation |
| Human gate (approval itself) | invalidated by policy tightening only | **none**; `GateExpired` is a marker (`store.py:328-331`) | CONFIRMED P1 |
| Gateway `InboxApprovalStore` | `reject` only; no revoke-after-approve | none | CONFIRMED P1 (probe) |
| `DevTokenVerifier` | no revoke | none (docstring says so) | dev only |
| OAuth gateway tokens | revocation exists | see `agent_gateway/oauth.py` | `tests/test_agent_gateway_oauth.py:122` |
| `GrantStore` grants | `remove` effective next request (file re-read each `authorize`) | none | CONFIRMED P2 |
| Secrets | `revoke`/`rotate` on stores (`secrets/stores.py`) | broker TTL in `runtime_contracts` LocalCredentialBroker | `tests/test_secret_stores.py:73` |
| `UndoWindow` | n/a | 5 min default (`approvals.py:108`) | `tests/test_agent_gateway_approvals.py:90` |

Gap: after an approved human decision, a later `revoke` of the capability does not invalidate an approval whose node was already dispatched but is retrying. INFERRED P2.

Proposed fix: give approvals `expires_at` (event-sourced, default for example 1 h for tier 2 and 15 min for tier 3), refuse `approve` and dispatch past it, and re-park. Add `revoke_approval(mission, node)` appending `ApprovalInvalidated`. Add a `SharedStore` implementation (SQLite or Redis) for `ReplayGuard`.

### (e) Approval binding, replay, expiry, separation of duties

#### Mission stack to operator (`mission/approval.py`)
- Binding: token payload binds operator, capability, mission_id, node_id, inputs_sha256, marker, exp, nonce (`approval.py:100-113`). Verified with constant-time compare, fail closed (`approval.py:116-152`). CONFIRMED, tested (`tests/test_approval_token.py:37-65,162-222`).
- Tamper $100 to $1000 at the operator: refused because `inputs_hash` differs (`test_approval_token.py:58` `test_changed_inputs`). CONFIRMED.
- Replay: nonce consumed once (`approval.py:176-189`). CONFIRMED, tested (`:65`). Per-process only (see (d)).
- Unbound dimension: a token with a null `node_id` or `mission_id` verifies for any value (`approval.py:_binding_ok`). The client refuses to mint without mission (`operators.py:56-62`) but mints with `node_id=None` if `_node` is missing. Tested as intended semantics (`test_approval_token.py:222`). P2.
- Hash exclusions: `_mission`, `_goal`, `_node` and any `_approval*` key are excluded; nested approval keys are hashed as-is (documented). An attacker who controls `_goal` or `_mission` meta values inside inputs is not hashed. P2 INFERRED.

#### The human decision to the token (the actual gap)
- What the human approves: `HumanTask.evidence = [capability, side_effecting, produces]` (`runtime.py:1018-1019`), and the packet `proposed_action` has capability, operator, produces, side_effecting (`policy_decision.py:174-178`). No inputs, no amount, no recipient, no input hash. CONFIRMED P1.
- What the runtime records: `ApprovalGranted{node_id, edit, approver, capability}` (`runtime.py:1194-1206`). No `inputs_sha256`. `_approved()` re-checks node id and capability only (`runtime.py:1783-1830`). CONFIRMED P1.
- The mint happens later with current inputs (`operators.py:67-69`), so the token binds whatever the inputs are at dispatch. CONFIRMED.
- Approver `edit` is merged into the marker (`runtime.py:1002-1008`), so an approver can alter what the handler sees, and the edit is excluded from the input hash (it travels in the marker, which is signed, so it is bound once minted). Fine, but the edit is not shown back as a diff. P2.
- Self-approval by the LLM: external agents cannot call `/approve` with a normal gateway token: the gateway capabilities list contains no approve capability (`agent_gateway/capabilities.py` names: list/get/explain/list_pending/delegate/pause/resume). `missions.pause`/`resume` are exposed (bounded writes, P2). A client-supplied `_approval*` is stripped from `edit` (`mission/api.py:133`, tested `test_mission_api_default_deny.py:156,176`). INFERRED OK. However `ApprovalPolicy.approver` in the harness is an arbitrary callable `Approver(dict)->bool` (`agent_harness/tools.py:52,82`); if an integrator wires an LLM-backed approver the system cannot tell. P2 (document and forbid).
- Separation of duties: see summary item 3. The `InboxApprovalStore.approve(by=...)` takes a free string. `ExternalApprovalBridge.approve` requires a principal and compares it to the request's principal (`approval_bridge.py:82-92`), which is the opposite of separation: it demands that the approver IS the requesting principal. In `today.py:481-483` the approver principal is `req.identity.principal` and the attestation is the literal `"projects-session"`. Today's approvals are bound to the viewed digest, so tampering is closed, but SoD is a single operator. P1 for money or send flows.

#### Gateway `InboxApprovalStore` (probe-confirmed)
- Binding: `request.intent_hash()` covers capability, args, subject, tenant, workspace (`contracts.py:164-172`). Probe: approve $100 then call with $1000 returns PENDING_APPROVAL. CONFIRMED safe against tamper.
- Replay: after one approval the identical request returned OK three times (probe). CONFIRMED P1 (a CRITICAL send/charge can be re-run indefinitely unless the caller sets an idempotency key and the key is honored; the idempotency cache only covers `manifest.idempotent`).
- Expiry: `created_at` recorded, never checked. CONFIRMED P1.
- `approve(..., edit=...)` stores the edit but nothing applies or re-hashes it (probe printed `None`; `edit` is dead state). P2.
- Requester self-approval: probe called `approve(by="agent1")` for agent1's own request. CONFIRMED P1.

#### Other
- Fleet `Context.resolve` (`context.py:63-78`): no approver identity, no payload hash (payload is only `{"prompt": ...}`), non-atomic id allocation `_next_id` (count of lines + 1) and a read-modify-rewrite with no lock, so concurrent writers can lose updates. Its effect after approval is not wired to a payload at all (the approval is a flag the executor must re-read). P1 INFERRED (legacy).
- `ExternalApprovalBridge`: single use CONFIRMED (`approval_bridge.py:114-119`), post-approval mutation changes the digest CONFIRMED, tested in `tests/test_external_agent_gateway.py` and e2e. No expiry; `_scrub` removes keys named like secrets from the digest (`external/contracts.py:49-51`), so a changed value under such a key is not detected. P2.

Proposed fixes:
1. Record `inputs_sha256` and a rendered `action_summary` (amount, currency, destination, resource) in the `NodeParked` packet and `ApprovalGranted`. At dispatch, recompute and refuse if different (fail closed and re-park). Show the facts to the approver.
2. Make `InboxApprovalStore` entries single-use with `expires_at`, consumed on first satisfied `is_satisfied`; reject `approve` when `by == request.principal.subject`.
3. Require per-person approver credentials and refuse approver == requester in `runtime.approve`, comparing against `MissionCreated.principal`. Default `AGENTIC_OS_APPROVER_TOKENS` to required for tier 3 capabilities.

### (f) Privilege non-escalation across subagents and models

- Fork/join: a fork is "not a set of independently authorized agents"; the parent owns authority, budget and deadline; branches can never approve (`mission/fork_join.py:1-12`); branch node id embeds mission, fork and branch so a token cannot run another branch (`:50-57`); `stricter_failure_policy` can only tighten (`:99`); mission inputs never reach branches (`tests/test_mission_fork_join.py:38`). Tested: policy tightening invalidates approved branch (`:587`), revoked grant fails unstarted branch closed (`:601`). CONFIRMED good.
- Approval does not follow a re-bound capability (`runtime.py:1783-1830`, `tests/test_authorization_correctness.py:93`). CONFIRMED good. Legacy approvals without a recorded capability are grandfathered (`:139` test) which is a documented bypass for old logs. P2.
- Model switch: authority never derives from the model. The planner and compiler see only `PolicyScopedRegistry(policy_refs)` (`mission/runtime.py:433-436`, `mission/registry.py:157-185`). Model routing is by data class and mode, not by principal (`governance/routing.py`), and holding a provider key does not bypass it (`tests/test_governance.py:68`). In `OPEN` mode private data may go to an external provider, flagged `privacy_preserved=False` (`routing.py:118-121`). CONFIRMED. Nothing ties the model identity to the authority record: `InferenceReceipt` (`governance/receipt.py`) records model/provider/boundary but has no mission, principal or tenant. P2.
- No explicit parent-to-child scope narrowing for gateway-delegated missions: `delegate` passes no policy_refs, so the child gets `[]` (narrower than the caller, accidentally). The delegating gateway principal's scopes are not intersected into the mission grants. INFERRED P2.
- Cross-tenant: `OwnerOnlyAuthority` denies by default (`execution_context.py:129-146`); divergence is enterprise-only. Tested `tests/test_execution_context.py`, `tests/test_security_telemetry.py:83` (`test_delegated_authority_refused_at_boundary`). CONFIRMED. Caveat: `resolve_for_node` returns a decision the caller must honour; there is no test that every executor honours it. UNTESTED P2.

### (g) Secret exposure to model context, logs, traces, receipts

Good:
- Credentials flow by reference, never value: `ResourceRef.credential_ref` (`execution_context.py:39-40`), `CapabilitySpec.secrets` named refs (`mission/types.py:117`), redeemed JIT into an ephemeral `secrets` channel never put in the dedupe cache (`mission/executor.py:27-60,170-215`, `operator_sdk.py:147-199`), and the ledger stores refs not values (`tests/test_security_durable_sink.py:45`, `tests/test_workflow_discovery.py:124`, `tests/test_stack_knowledge.py:25 test_model_never_sees_secrets`).
- Contained sandbox child gets a scrubbed env (`mission/membrane.py:147-178`), tested by `test_membrane.py:46` (see run results; fails on macOS here for a `preexec_fn` reason).
- Stores: encrypted-file refuses symlink and group/world-readable (`secrets/stores.py`), 0600 write; Vault/keyring backends. Default backend is `env` (`secrets/factory.py:build_secret_store`), development grade, and the credential broker asserts `production` only for non-env stores. CONFIRMED.
- Gateway egress: `DataClass.SECRET` is always denied, PII redacted, sensitive-key and secret-shaped values masked (`agent_gateway/egress.py:23-110`), tested (`tests/test_agent_gateway_egress.py:24-112`). Approval tokens never put in gate notifications (`gate_notify.py` docstring, `tests/test_gate_notify.py:322`). Approval secret never logged (`operator_sdk.py:_warn_unverified_approval`).
- Egress filter `_redact_str` covers email, `sk-/xox` keys, Bearer, JWT-ish, 32+ hex, card-like digit runs.

Gaps:
- P2 CONFIRMED: `NodeSucceeded` stores the full handler `result` (`mission/runtime.py:862-863`) with no redaction; any secret or PII in a result lands in the event log, the replay data and the cockpit.
- P2 CONFIRMED: gateway default egress is allow-all (`gateway.py:57-59,114`); only the read-only mount sets `PolicyEgressEngine` (`projects_api.py:1393`). A new `AgentGateway(...)` without `egress=` leaks. The sandbox and write registries have no egress rule.
- P2 CONFIRMED: `except Exception as exc` returns `str(exc)` to the external client (`gateway.py:195-197`, `client_view` ERROR includes `error`, `contracts.py:223-224`). Exceptions often embed URLs, tokens or paths.
- P1 INFERRED: `MCPClient.stdio` copies the entire parent env into the MCP server subprocess (`context-runtime/.../tools/mcp.py:40`). Every ambient secret reaches an arbitrary tool server.
- P2 CONFIRMED: `ToolRegistry.audit` stores raw `args` of side-effecting calls in memory (`agent_harness/tools.py:150,154`). If args carry secrets the audit list holds them.
- P2 CONFIRMED: `redact` is pattern-based (`egress.py:35-43`) and does not cover base64 blobs, short API keys, `AKIA...` AWS keys or private keys in free text.
- P2: the approval token travels in the HTTP body (not a header) to operators and is not a bearer secret beyond 300 s and one use; fine.
- `envelope`/`GovernedEnvelope` carries intent hash, not secrets. OK.
- Key at-rest: `GrantStore` falls back to a generated on-box key beside the data with a warning, or plaintext when `cryptography` is missing (`permissions.py:134-166`). P2.

### Operation inventory with risk tiers

Tier scheme (matches `RiskTier`, `agent_gateway/contracts.py:34-39`): T0 read, T1 bounded or reversible write, T2 consequential or external, T3 critical (financial, destructive, legal, security). "Gate" is what the code enforces today.

#### Gateway-exposed (agent_gateway)

| Operation | Class | Tier | Gate |
|---|---|---|---|
| projects.list, projects.get_status, missions.list/get/explain, missions.list_pending_approvals, sources.search, crm.lookup_account (`capabilities.py:46-74`) | read | T0 | permission/scope, egress redact |
| missions.delegate_goal (`capabilities.py:129`) | delegate | T2 | permission; side effects gated inside mission |
| missions.pause / resume (`capabilities.py:140-144`) | admin/control | T1 | permission |
| crm.prepare_outreach (`write_capabilities.py:39`) | read-ish | T0 | permission |
| crm.request_outreach_send (`:49`) | send | T2 | REQUIRED approval, idempotent |
| projects.request_change (`:62`) | write | T2 | REQUIRED approval |
| sandbox.execute (`sandbox.py`) | exec | T3 | MANDATORY approval, containment |
| external agent / social operators (`external/operator.py`, `social/operator.py`) | send/publish | T2 | ExternalApprovalBridge digest |

#### Operator capabilities (apps/*, from `side_effecting=True` plus `approval_required`)

| Capability | Class | Tier | Approval flag in code |
|---|---|---|---|
| billing.charge, billing.dunning, billing.refund, billing.create_subscription, billing.cancel_subscription (`apps/billing/operator.py:79-120`) | payment/refund/cancel | T3 | approval_required (dunning, refund, create); cancel gated in core by `_approval` |
| books.close_period, books.reverse_entry, post entry (`apps/books/operator.py:35-76`) | ledger write | T3 | approval_required on close; reversal gated in core |
| privacy.delete (erasure), privacy.export, privacy write (`apps/agentic-privacy/operator.py:50-72`) | delete/export | T3 | delete approval_required; export is `side_effecting` with `privacy:read` |
| infra.configure, infra.destroy_delta, infra.rollback_release, infra.provision (`apps/infra/operator.py:123-139`) | admin/destroy | T3 | `gated()` |
| sky.launch / down / serve / serve_down (`apps/sky`) | infra spend/destroy | T3 | verify per cap |
| outreach.send_all, lead sync (`apps/outreach-engine`) | send | T2 | approval_required |
| social.publish (`apps/social-autopilot`), video.publish/unpublish (`apps/video-ad`) | publish | T2 | approval_required (publish), unpublish has none |
| support.resolve / escalate / send_onboarding (`apps/support`) | send/write | T2 | `gated()` |
| crm.qualify, crm.delete_activity, crm.retract_opportunity (`apps/agentic-crm`) | write/delete | T1/T2 | `gated()` on qualify, others per spec |
| compliance policy_change, consent filing (`apps/compliance`) | legal | T3 | approval_required |
| growth.budget_change (`apps/growth-engine`) | money move | T3 | approval_required |
| edge-sentinel.block_ip / unblock_ip (`apps/edge-sentinel`) | security admin | T2/T3 | block approval_required; unblock read-back |
| lifecycle campaign compose (`apps/lifecycle`) | draft write | T1 | approval_required=False by design (human sends in Listmonk) |
| document-editor write/publish (`apps/document-editor`) | write/publish | T1/T2 | mixed (two True, two False) |

Not exhaustively verified per capability: operators outside the apps listed (market-radar, growth-assistant, zoning, content/revenue fleets). Treat as UNTESTED P2: add a conformance test that every `side_effecting` capability with T2+ derived from spec is `approval_required` or wrapped in `gated()` (see gap G9).

#### Platform and control-plane operations

| Operation | Class | Tier | Gate |
|---|---|---|---|
| `POST /missions` create (`mission/api.py:80`) | write | T1 | control token, tenant limits |
| `POST /missions/{id}/approve` | approve | T3 control | token; SoD only if approver tokens configured |
| compensation retry/approve/acknowledge/verify_manually (`mission/api.py:148-165`) | admin | T2 | approver credential required when configured |
| `change_policy` (grant/revoke/budget) (`runtime.py:1303`) | admin | T3 | governance API; verify caller auth separately |
| `POST /api/permissions/grants` add/remove (`control_plane.py`) | admin | T3 | PERMISSIONS_ADMIN_KEY (shared) |
| `control_plane` `/up /down /dispatch` and `/approvals/{id}/{decision}` (`control_plane.py:500-514`) | deploy/approve | T2/T3 | single API key |
| operator `POST /invoke` (`operator_sdk.py:259`) | direct exec | by capability | bearer token; approval token |
| secrets init/push/rotate (`secrets/bootstrap.py`) | admin | T3 | local CLI |
| `UndoWindow.undo` (`approvals.py:129`) | compensation | T1 | no principal check; any holder of the entry id can undo (P2) |

### Scenario coverage

Tests run with `/private/tmp/claude-501/.../scratchpad/venv` (python 3.13, has fastapi): `PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider ...`. Results:

- Run 1 (approval_token, gateway approvals/egress, authorization_correctness, tenant_limits, policy_scope, mission_api_default_deny, secret_stores, rehydrate_authority, external_agent_gateway, execution_context, mission_policy, policy_decision, security_telemetry, membrane): 239 passed, 5 failed. All 5 are `tests/test_membrane.py` (`subprocess.SubprocessError: Exception occurred in preexec_fn`), an environment issue (RLIMIT setup in `preexec_fn` on macOS arm64); not a product regression. INFERRED environmental.
- Run 2 (governance, oauth, fork_join, conformance_forgery, strip_approval, stack_knowledge, agent_gateway*, e2e, operator_sdk, gate_notify, security_durable_sink): 269 passed, 80 skipped, 2 failed: `test_agent_gateway_sandbox.py::test_default_backend_runs_a_capability_in_real_containment` (same preexec cause) and `test_security_durable_sink.py::test_durable_and_queryable_on_duckdb_backend` (duckdb backend, not investigated; likely a missing optional dependency).

#### Scenario 1: privilege laundering across models
Existing: partial. `tests/test_governance.py::test_api_key_does_not_bypass` (68), `::test_private_data_never_routes_external_even_for_coding` (48), `::test_fail_closed_when_no_compliant_route` (56), `::test_open_mode_allows_private_to_external_but_flags_it` (101); `tests/test_policy_scope.py::test_mission_blocked_when_a_needed_capability_is_forbidden` (30). All pass.
Gap G1 (UNTESTED, P2): nothing proves that changing the executing model or provider on a mission leaves `policy_refs`, approvals and `PolicyEvaluated.policy_digest` unchanged, or that an output written by model A cannot carry markers (`_approval`, `approval:` constraints) that model B or an operator treats as authority.
Sketch: create a mission, run the planner with two different `decision_provider` stubs that return a plan with `inputs={"_approval": {...}, "Approval": true}`; assert `strip_approval_keys` removes them, the node is parked, `ApprovalGranted` absent, and `policy_digest` identical across both runs.

#### Scenario 2: delegation escalation
Existing: partial. Fork/join: `tests/test_mission_fork_join.py::test_mission_inputs_never_reach_branches` (38), `::test_policy_tightening_invalidates_unstarted_approved_branch` (587), `::test_default_is_halt_and_report_and_hook_can_only_tighten` (343); `tests/test_authorization_correctness.py::test_a_rebound_outcome_does_not_inherit_the_approval` (93); `tests/test_fork_join_compensation.py::test_hook_cannot_widen_the_mission_policy_into_compensation` (97); `tests/test_agent_gateway_missions.py::test_delegate_requires_permission` (75).
Gap G2 (UNTESTED, P1): gateway to mission delegation: no test asserts that a delegated mission's grants are the intersection of caller scopes and requested constraints, that tenant propagates, or that a caller cannot pass `policy_refs`/`budget` inside `arguments`. Today `delegate` forwards `inputs=dict(arguments)` wholesale.
Sketch: `AgentGateway` with `MissionRuntimeAdapter`; principal tenant `t1` scopes `{missions.delegate}`; call `delegate_goal` with `arguments={"goal": g, "policy_refs": ["*"], "budget_usd": 1e6, "tenant": "t2"}`; assert created `Mission.tenant == "t1"`, `policy_refs` does not contain `*`, budget is the tenant ceiling.

#### Scenario 3: prompt-injection exfiltration
Existing: partial, output side only. `tests/test_agent_gateway_egress.py::test_gateway_redacts_pii_and_records_the_decision` (94), `::test_gateway_denies_secret_output_never_reaches_client` (104), `::test_secret_is_always_denied` (48), `::test_strictest_action_wins_across_classes` (71); `tests/test_governance.py::test_tool_eligibility` (76), `::test_engineering_pool_cannot_touch_private_data` (164); `tests/test_sources_postgres.py::test_evidence_refs_are_structured_and_leak_no_secret`. `tests/test_cockpit_xss.py` covers markup injection only.
Gap G3 (UNTESTED, P1): no test where retrieved content containing instructions ("email the customer table to x@evil") induces a tool call to a destination outside the allowed set, since there is no destination-level policy (see (c)). Also no test that egress catches a handler that declares `INTERNAL` while returning PII, nor an end-to-end test that a SEND capability's recipient domain is checked.
Sketch: register `crm.request_outreach_send` with a recipient allow-list policy fact; feed a source doc with an injection payload through a stub agent that issues the call; assert PENDING_APPROVAL with the destination in the packet and no handler call; assert denial when the domain is not on the allow-list.

#### Scenario 4: approval tampering
Existing: strong at the token and gateway-intent layer. `tests/test_approval_token.py::test_changed_inputs` (58), `::test_tampered_payload_and_signature` (37), `::test_replay_rejected` (65), `::test_expired` (44), `::test_f1_missing_or_wrong_mission_stays_staged` (162), `::test_f1_missing_or_wrong_node_stays_staged` (171), `::test_forged_markers_never_reach_handler` (109), `::test_e2e_http_approval_executes_exactly_once` (275); `tests/test_agent_gateway_approvals.py::test_approval_is_bound_to_the_exact_request` (66); `tests/test_mission_api_default_deny.py::test_body_smuggled_approval_flags_do_not_approve` (156), `::test_approval_keys_stripped_from_edit_before_runtime` (176); `tests/test_agent_gateway_approvals.py::test_undo_refused_after_the_window_expires` (90); `tests/test_authorization_correctness.py` (node and capability binding). All pass.
Gap G4 (UNTESTED, P1): (i) mutation between park and resume: no test that changes `$from_world` or mission inputs after `NodeParked` and asserts the approval is void. This FAILS today by design (summary item 1). (ii) `InboxApprovalStore` single-use, expiry and self-approval (probe shows all three fail). (iii) approval after `GateExpired`. (iv) `edit` that raises the amount.
Sketch (i): park `billing.refund` with `amount_cents=10000`, then `world.observe("refund_amount", 100000)`; approve; assert dispatch refuses or re-parks (expected after the fix: `ApprovalGranted.inputs_sha256 != current`).
Sketch (ii): reuse `probe_b.py`: assert the second identical call returns PENDING_APPROVAL, `approve(by=requester)` raises, and an approval older than TTL does not satisfy.

#### Scenario 7: revocation mid-run
Existing: good for mission grants. `tests/test_authorization_correctness.py::test_a_policy_change_invalidates_it` (153); `tests/test_mission_fork_join.py::test_policy_tightening_invalidates_unstarted_approved_branch` (587), `::test_revoked_capability_grant_fails_unstarted_branch_closed` (601); `tests/test_rehydrate_authority.py::test_revoked_grants_stay_revoked` (25); `tests/test_agent_gateway_oauth.py::test_revocation` (122); `tests/test_secret_stores.py::test_encrypted_file_rotate_and_revoke` (73); `tests/test_connections.py::test_disconnect_revokes_and_clears` (65). All pass.
Gap G7 (UNTESTED, P1): (i) revoke of a gateway principal's grant or OAuth token between `PENDING_APPROVAL` and retry (`is_satisfied` does not re-check, though `invoke` re-runs `is_visible` each call, so likely safe: INFERRED; add the test); (ii) revoke while a node is running (not interrupted, by design; assert documented behavior and that the next node is blocked); (iii) a revoked secret mid-run (broker `redeem` after `revoke` must fail).
Sketch: gateway with `DevTokenVerifier`; park request, then remove grant from `_grants`, approve, retry, assert DENIED `unknown_capability` and zero handler calls.

#### Scenario 14: secret hygiene
Existing: good. `tests/test_membrane.py::test_contained_capability_cannot_see_ambient_secret` (46) and `::test_explicit_passthrough_is_the_only_way_in` (environment failure here); `tests/test_secret_stores.py` (store hardening); `tests/test_secrets_bootstrap.py::test_push_reads_secrets_file_and_never_prints_values` (440); `tests/test_security_durable_sink.py::test_credential_event_stores_refs_not_the_secret` (45); `tests/test_stack_knowledge.py::test_model_never_sees_secrets` (25); `tests/test_gate_notify.py::test_http_json_never_leaks_token_in_error` (322); `tests/test_sources_drive.py::test_uses_the_oauth_token_and_never_leaks_it` (71); `tests/test_external_agent_gateway.py::test_secrets_never_enter_the_digest_or_canonical_form` (41); `tests/test_workflow_discovery.py::test_no_raw_secret_is_stored_in_observations` (124).
Gap G14 (UNTESTED, P2): no canary-secret test across the whole run. Sketch: set `CANARY=sk-test-AAAA...` in env and in a credential store, run a mission with a capability whose handler echoes it in result and in an exception; capture event log, gateway AuditEvent, `client_view`, logs (caplog), traces and receipts; assert the canary string appears in none. Today expect failures at `NodeSucceeded.result` (`runtime.py:862`) and `gateway.py:196`.

### Findings table

| # | Finding | Label | Sev | Evidence | Fix |
|---|---|---|---|---|---|
| F1 | Approver sees no payload; approval keyed by node+capability, not input hash; token mints from current inputs | CONFIRMED | P1 | `runtime.py:1018,1194-1206,1783`; `operators.py:67-69`; `policy_decision.py:174-178` | store `inputs_sha256` + action summary at park, verify at dispatch |
| F2 | `InboxApprovalStore`: no expiry, unlimited replay, self-approval | CONFIRMED (probe) | P1 | `approvals.py:53-77`; probe_b | single-use, TTL, approver != requester |
| F3 | SoD off by default; approver is a shared tenant token; actor not a person | CONFIRMED | P1 | `api.py:127-136`; `test_mission_api_default_deny.py:319` | per-person approver creds; compare to requester |
| F4 | Spend policy uses static `node.cost.usd`, not payload amount; no destination or resource facts | CONFIRMED | P1 | `policy_decision.py:154-157`; `policy.py:78-87` | payload-derived facts in `NodeContext` |
| F5 | No agent_id / run_id / delegation parent; `authority_chain` never populated; delegate drops tenant and principal id | CONFIRMED | P1 | `execution_context.py:56-57`; `mission_adapter.py:42-50`; `runtime.py:317` | propagate identity chain |
| F6 | Human gate never expires (`GateExpired` is notice only) | CONFIRMED | P1 | `store.py:328-331` | enforce approval TTL |
| F7 | MCP `readOnlyHint` from the server decides side_effecting; stdio inherits full env | INFERRED | P1 | `context-runtime/.../mcp.py:40,194-195` | default side_effecting for mounted servers; pass minimal env |
| F8 | `make_authorizer` checks only `read` and passes unmapped tools | CONFIRMED | P1 | `permissions.py:346-352` | check tool's declared action; default-deny unmapped |
| F9 | Fleet `Context` approvals: no approver identity, no payload hash, racy ids/rewrite; `needs_approval` by action name | INFERRED | P1 (legacy) | `context.py:63-78,_next_id`; `fleet.py:102` | migrate to mission approvals or add lock+hash |
| F10 | Approval/idempotency replay guard per process; `SharedStore` unimplemented | CONFIRMED | P2 | `approval.py:140-189` | implement shared store |
| F11 | Gate token node_id/mission_id may be unbound (null) and still verify; meta keys excluded from hash | CONFIRMED | P2 | `approval.py:_binding_ok`, `:12` docs | require node binding for tier 2+ |
| F12 | NodeSucceeded stores unredacted results; gateway returns raw exception text; default egress allow-all | CONFIRMED | P2 | `runtime.py:862`; `gateway.py:57,114,196` | redact at ledger boundary; generic errors; default PolicyEgressEngine |
| F13 | `GrantStore` grants have no expiry/tenant; unauth reads of grants; admin role bypass; on-box key fallback | CONFIRMED | P2 | `permissions.py:48-70,134-166,274`; docs | add `expires_at`, tenant; authenticate reads |
| F14 | `ToolRegistry.get(name).run()` bypasses authorizer and approval | INFERRED | P2 | `agent_harness/tools.py:125,137` | return a bound wrapper only |
| F15 | `UndoWindow.undo` needs no principal | CONFIRMED | P2 | `approvals.py:129-139` | bind to requester or approver |
| F16 | Legacy approvals without capability are grandfathered | CONFIRMED | P2 | `runtime.py:1800-1805`; test 139 | sunset after migration |
| F17 | `ExternalApprovalBridge` attestation is a literal string; `_scrub` drops secret-named keys from the digest | CONFIRMED | P2 | `today.py:481-482`; `external/contracts.py:49-51` | real attestation; hash a keyed digest of scrubbed values |
| F18 | Model identity not part of authority/receipt records | INFERRED | P2 | `governance/receipt.py:17-28` | add mission/principal/tenant to `InferenceReceipt` |

### Proposed test gaps (priority)

G4 (approval mutation between park and resume; Inbox single-use, TTL, self-approval) and G2 (gateway to mission delegation grants/tenant) first. Then G3, G7, G1, G14, and G9 (conformance: every spec-level T2+ side-effecting capability is `approval_required` or `gated()`).

## Workstream C: execution integrity audit (read-only)

Repo: /Users/arybach/agentic-os, branch audit/agent-security-reliability-2026-10-23. Date 2026-10-23.
Labels: CONFIRMED (read code and/or ran it), INFERRED (code reading only), UNTESTED. Severity P0 (can cause an unrecorded or duplicate real-world side effect or false "done" in a production path), P1, P2.
Paths below are relative to /Users/arybach/agentic-os/agentic_os unless stated.

Tests run (venv-ent, py3.12, offline, fsync off): tests/test_operator_sdk.py test_mission.py test_integration_phase1_events.py test_revenue_restart.py test_reliability_order.py test_verification.py test_mission_concurrency.py test_compensation_authorization.py test_rehydrate_outcome.py test_event_log_reproducibility.py test_approval_token.py test_integration_obligation.py test_experiment_payout_reconciliation.py: 153 passed. The placeholder-guard test (a copy of agent-harness-dev, run from the scratchpad) passed 25/25. Probe script: scratchpad/probe_c.py.

### 1. Summary of headline gaps

| # | Gap | Sev | Label |
|---|-----|-----|-------|
| C1 | No UNKNOWN / "outcome ambiguous" state anywhere in the mission or Today lifecycle. A timeout after a possible side effect becomes FAILED (mission) or a stuck item (Today). No reconcile-before-retry step. | P0 | CONFIRMED (probe) |
| C2 | Verification is not independent. `verify_result` and `CompositeVerifier` judge the provider's or operator's own returned dict. A fabricated `artifact_refs` string yields SUCCEEDED + delivery_verified. | P0 for any real adapter (the only adapter today is the fake) | CONFIRMED (probe) |
| C3 | Idempotency/dedupe stores are in-memory per process (Operator `_seen`, `ReplayGuard`, `Executor._ledger`, `EventInbox._seen`, `TaskManager._by_idem`). Crash between NodeDispatched and NodeSucceeded re-dispatches the node, and the dedupe cache is gone. | P0 once more than one process or a restart is involved | CONFIRMED by code; replay not executed |
| C4 | Exceptions and failure-shaped results are never cached by design (`operator_sdk.py:223`). The runtime has no retry or timeout policy and no UNKNOWN. A timeout after commit followed by a manual or app-level retry re-executes the side effect. | P1 | CONFIRMED |
| C5 | Receipts and the event log are not tamper-evident (no hash chain, no signature). | P1 | CONFIRMED |
| C6 | Trace recorder stores full, unredacted prompts, tool args and tool results in plain blobs with a 64-bit truncated hash. | P1 | CONFIRMED |
| C7 | Placeholder-contamination guard exists with a regression test, but only on the unmerged branch `fix/context-correctness-observability` of agent-harness-dev. It is absent from the agent-harness main/ours-h2 checkout. It is bypassed by nested args and by a stub not at the start of a string. | P1 | CONFIRMED |
| C8 | Deadlines: `reliability.py` ExpectedTransition/monitor_transitions is wired only into commercial/order.py. Mission runtime, Today and the executor have no deadline, timeout or cancellation of in-flight work. | P1 | CONFIRMED |
| C9 | No requested-artifact-format verification (type, extension, magic bytes) on the completion path. | P1 | CONFIRMED (absence) |
| C10 | Approval in the mission path is bound to node+capability, not to the inputs the human saw. It has no expiry (GateExpired is notice-only). | P1 | INFERRED |

### 2. Lifecycle states

Requested: PROPOSED, AUTHORIZED, AWAITING_APPROVAL, EXECUTING, VERIFYING, SUCCEEDED, FAILED, BLOCKED, UNKNOWN. No single state machine carries them. Four partial ones exist.

- MissionState (mission/types.py:37-49): PLANNING, SIMULATING, RUNNING, WAITING_HUMAN, PAUSED, FAILED, SUCCEEDED, CANCELLED, COMPENSATION_FAILED.
- NodeState (types.py:52-60): PENDING, READY, RUNNING, WAITING, DONE, FAILED, SKIPPED, COMPENSATED. `node_status` (mission/store.py:296-312) folds NodeDispatched to RUNNING and NodeSucceeded to DONE.
- Today (today.py:34-35): STATES `needs_action|waiting|scheduled|running|verified|failed|dismissed`. LIFECYCLE `suggested, approved, sent, delivery_verified, outcome_observed`. This is the best-designed one. It keeps sent and verified apart on purpose.
- External task (agent_gateway/external/contracts.py:~78): TaskState, with WAITING_FOR_INPUT, WAITING_FOR_APPROVAL, terminal states, and a `CapabilityStatus.UNKNOWN` (capability support, not outcome). Receipts carry status SUCCEEDED, FAILED or HELD.

| Requested | Exists? | Where / note |
|---|---|---|
| PROPOSED | partial | Today "suggested"; `CandidateStatus.PROPOSED` in projects/contracts.py:143 (a different concept). Mission has PLANNING. |
| AUTHORIZED | partial | Event `ApprovalGranted` and `PolicyEvaluated`; no state. Today "approved". |
| AWAITING_APPROVAL | yes | `WAITING_HUMAN`; `NodeState.WAITING`; `pending_approval` results. |
| EXECUTING | yes | `RUNNING`; NodeDispatched. |
| VERIFYING | missing | Verification runs inline in `_finish` (runtime.py:~820). No persisted state or event between dispatch and result. An ESCALATE parks as WAITING. |
| SUCCEEDED | yes | |
| FAILED | yes | Conflates "did not happen" with "may have happened" (see UNKNOWN). |
| BLOCKED | partial | `_block_denied`; `AttentionKind.BLOCKED` in world/attention.py:32; bootstrap.BLOCKED (device posture). Not a node state. |
| UNKNOWN | missing | The only UNKNOWNs are observation `KnownAtQuality.UNKNOWN` (observation.py:32), social `Signal.UNKNOWN`, capability-status UNKNOWN, and `integration/provider.py:32`. None is an outcome state. verify.py FAILED_STATUSES even lists "timeout", "partial" and "unavailable" as failure. |

Scenario evidence for the UNKNOWN gap:
- Mission path: any operator exception, including a `BridgeError` from a timeout (mission/bridge.py:75-76, which drops the cause with `from None`), goes to `_fail_node` (runtime.py:792-802). It appends NodeFailed, runs the saga (compensation), and sets the mission FAILED. Nothing records that the effect may have happened. The saga may run an undo for an effect that did not occur. `Executor.compensate` skips when the forward step has no executed evidence, which is safe, but the remote timeout case leaves a real effect plus a FAILED mission.
- Today path (probe, CONFIRMED): an adapter that commits and then raises TimeoutError out of `submit_task`. The exception escapes `Today.approve` (today.py:486, not wrapped). Resulting item: state `needs_action`, lifecycle approved=True, phase `suggested`, sends=0. A retry with the same digest raises `ApprovalError: approval already consumed` (approval_bridge.py:115), which is not a TodayError. Provider effects: 1. Outcome: no duplicate, but the item is stuck, shows the wrong state, and has no reconcile path. Only `status != SUCCEEDED` on a returned receipt is handled (today.py:502-507, "Not confirmed sent — check before retrying"). That is a HELD receipt, mapped to "failed", and `attempt += 1` makes a fresh approval produce a new idempotency key `f"{item_id}:{c.attempt}"` (today.py:325). The text tells the human to check, but nothing enforces it. A re-approval after an unconfirmed send issues a new task and can double-send.

Proposed fix (C1, P0):
1. Add `ExecutionOutcome.UNKNOWN` (a NodeState and a receipt status).
2. Classify transport timeout, 5xx after send, and process crash with a NodeDispatched marker but no result as UNKNOWN. Never FAILED, and never run the saga on UNKNOWN.
3. Park the node as BLOCKED/WAITING with a `reconcile` task. The task calls a read-only provider lookup by idempotency key or external id.
4. Only the reconcile result (found => SUCCEEDED, absent => FAILED, retry allowed) may move the node.
5. In Today, wrap `approve` in try/except, record phase `unknown`, and refuse re-approve until `reconcile()` has run.

### 3. Idempotency, retries, timeouts, concurrency, checkpoint/resume, deadlines, cancellation, compensation

**Dispatch idempotency.**
- The key is `sha256(mission:revision:outcome)[:16]` (mission/compiler.py:46-47). It is stable per node across restart and revision, and it is sent as an `Idempotency-Key` header and body field (mission/operators.py:48-74, bridge.py:70).
- Operator-side dedupe (mission/operator_sdk.py:146-184) is on `(capability, canonical inputs, key, secret ctx)`. It has in-flight coalescing: concurrent identical calls wait and share one execution (300s wait, then TimeoutError). Covered by tests/test_operator_sdk.py (`test_operator_invoke_dedupes_on_idempotency_key`, `test_idempotency_key_scoped_to_capability_and_inputs`, `test_approved_retry_is_not_served_the_cached_staged_result`, `test_tenant_tokens_do_not_share_idempotent_results`). CONFIRMED passing.
- Limits (CONFIRMED by code):
  - The cache is in-memory and LRU-bounded (cap 10000, `AGENTIC_OS_IDEMPOTENCY_MAX`). `ReplayGuard` for approval nonces is per-process and carries the comment "SEAM: per-process; multi-replica needs a shared store" (approval.py:180-204), with `warn_if_replicated`. A restart, a second replica, or LRU eviction means the same key re-executes the handler. The key is not honoured by the real provider unless the handler passes it through.
  - Only successes are cached. Exceptions and failure-shaped results are not (operator_sdk.py:221-230). That is correct for "retry after a clean failure" but wrong for "ambiguous".
  - The in-process mission `Executor._ledger` (executor.py:~190) exists only for compensation evidence.
  - The InMemoryOperatorClient dedupe (executor.py:42-62) is a test double.
  - The mission-level `_run_lock` is a `threading.RLock` per process (runtime.py:627-643). The Postgres event store is shared across instances (event_backends.py:110), but no cross-process lock or lease protects `run()`. Two instances can fold the same log and both dispatch a ready node. INFERRED, P0 for multi-instance deployments. Fix: DB advisory lock or lease row per mission, and a conditional append `NodeDispatched` with a unique (mission, node, attempt) constraint, executed before invoke.

**Crash between dispatch and result (checkpoint/resume).** `NodeDispatched` is appended before invoke (runtime.py:785, 909). After a crash `node_status` returns RUNNING, which is not in `_TERMINAL` (runtime.py:53). `run()` (runtime.py:645-711) rebuilds `done` from terminal states only, so the RUNNING node returns to the scheduler frontier (scheduler.py:43-49 only skips done/running IDs passed in, and `running=set()` is passed) and is re-dispatched with the same key against a fresh operator process. CONFIRMED by code; not executed. Rehydrate-from-log tests exist (test_rehydrate_outcome.py, test_revenue_restart.py::test_restart_then_owner_completes_the_rehydrated_mission, test_event_log_reproducibility.py), and none covers a crash in the dispatch window. P0, see C3. Fix: on rehydrate, a node with NodeDispatched and no terminal event becomes UNKNOWN (reconcile), not auto re-run.

**Retries.** The mission runtime has no retry or backoff policy. `node.attempts` is incremented (executor.py:run) but never compared to a limit. Any exception fails the mission (runtime.py:792). Retry therefore only happens as an application re-run or a Today re-approve. For `post_json`, there is only a timeout (util.py, 60s default, operators.py:36) and no retry. P2 (fail-closed is safe), but it makes UNKNOWN essential.

**Timeouts.** HTTP clients have a 60s timeout (operators.py). The `bridge.HTTPOperatorClient` transport takes no timeout argument at the call site (bridge.py:74). There is no per-node or per-mission timeout in `Executor.run` or `_execute_wave` (the ThreadPool join at runtime.py:~925 waits forever for `as_completed`). A hung handler blocks the mission and holds its run lock. P1. Fix: node deadline, `future.result(timeout)`, then UNKNOWN.

**Concurrency.** Resource-key serialization (mission/concurrency.py) is sound, with conservative over-serialization of unresolved templates. Waves commit results serially in node order (runtime.py:~930). Approval serialized with run via the per-mission lock, and a double approval fails with `ApprovalError "no pending approval"` (runtime.py:1156-1165). Tests: test_mission_concurrency.py, test_concurrent_execution.py. Ok in-process (multi-process: see above).

**Deadlines.** `ExpectedTransition`/`evaluate_transition`/`monitor_transitions` (reliability.py:63-94) are pure and tested (test_reliability_order.py::test_transition_overdue), but are used only in commercial/order.py:61, service.py:96 and receivables.py:92. `Node` has no deadline. `run_batch(deadlines=...)` only orders scheduling (resource_scheduler.py:78). Today `_Case.deadline` is display only (today.py:81, 359). Nothing emits an incident for "deadline near, no verified completion" on a mission or Today item. Gap, scenario 12.

**Cancellation.** `cancel_mission` (runtime.py:371-388) records MissionCancelled and settles compensation if done side-effecting nodes exist. There is no interruption of an in-flight node (the thread pool and the remote call keep running), and no late-result fence for the mission path. The external TaskManager does handle it correctly: cancel is final, and a later provider SUCCEEDED cannot flip the state (agent_gateway/external/lifecycle.py:~108-122, CONFIRMED by reading; the test is in the external gateway suite, not re-run). Mission path P1: a result landing after cancel is `_finish`ed only if the wave join returns, and no check of the cancelled state precedes commit (INFERRED).

**Compensation.** Strong. Evidence-bound (durable log probe), registry-declared undo only, own-approval option, idempotent per `{key}:undo:{attempt}`, COMPENSATION_FAILED terminal attention state, never inferred as rolled back (executor.py:compensate, compensation_runner.py, receipt.py). Tests: test_compensation_authorization.py etc. (passing). One concern: compensation is triggered by failure, including ambiguous failure (see C1), and `proves_execution` is derived from the result dict, which for a timeout is absent. Keep as is, but gate it behind UNKNOWN resolution.

### 4. Independent postcondition check

Not present for external side effects.
- `CompositeVerifier.verify` (mission/verify.py:140-188) checks: result contains the declared outputs, failure-shaped denylist (`FAILED_STATUSES`), declared output values truthy, permissions still granted, and `StateAssertion`s against the result or world. All of it reads the operator's own return value. The docstring says "no consequential operator result becomes authoritative World State until the acceptance policy succeeds", but the check is on the claim.
- `external/verification.py` `verify_result`: binds the result to the request digest, then VERIFIED if `artifact_refs` is non-empty. It never queries the provider or a system of record. CONFIRMED by probe (scenario 10): an adapter returning `artifact_refs=("artifact:made-up",)` produced receipt SUCCEEDED, state `waiting`, `delivery_verified: True`.
- Read-after-write exists in a separate plane: the Obligation engine (integration/sync.py, tests/test_integration_obligation.py, test_integration_phase1_events.py::test_durable_loop_dropped_write_detected_then_replayed_to_satisfaction), which is a genuine independent check, but it is not what gates mission node completion or Today.
- `NodeSucceeded` is appended after the inline verify, with no distinct event for verification start. `VerificationRecorded` is logged (evidence.py:20).

Fix (C2, P0): add a `PostconditionProbe` SPI, `probe(receipt) -> FOUND|ABSENT|ERROR`, called by an independent read path (provider message-status API, ledger lookup, file open and parse). Success requires FOUND. ERROR gives UNKNOWN. For the external adapter, make `ExternalAgentAdapter.verify_effect(ref)` mandatory for side-effecting capabilities and treat `artifact_refs` as claims. `CompositeVerifier` should require an `assertion` sourced from `probe`, not from `result`, for any node with `side_effecting=True`.

### 5. Receipts and tamper evidence

- `mission_receipt` (mission/receipt.py:20-54) folds the event log: steps with full results, approvals (seq, node, capability, decision; no approver, no input hash), outcome, compensation, evidence ref ids. No hash, no signature, no previous-receipt link. It is recomputed from the log on every call.
- Event log: `Event` has seq and ts only. The JSONL, DuckDB and Postgres stores (mission/store.py, event_backends.py:55-134) are append-only by convention. No chain hash, no signing, and no DB trigger or revoked UPDATE/DELETE grant. Anyone with file or DB write can edit history undetectably. CONFIRMED (no hash in store.py/event_backends.py).
- `runtime_contracts.models.execution.ExecutionEnvelope` (content-addressed `binding`, optional detached `signature`) and `ExecutionReceipt` (envelope_binding, idempotency_key, outcome `executed|refused|failed`, `side_effect_digest`, `attestation`, `receipt_id = content_hash`) are the best design: per-receipt tamper-evident via content addressing, bound to the authorizing envelope. Gaps: not hash-chained to each other, `attestation` empty for the local membrane ("empty is honest"), outcome set has no `unknown`, and `mission_receipt` does not use `ExecutionReceipt` (only membrane.py:88,207 and execution_backend.py use it; ExecutionReceipts are not appended to the mission log). `ActionReceipt` (projects/contracts) in external/operator.py has status, external_id, error, decision_id, with no hash binding to the request digest.
- `InterventionRecord` (intervention_record.py:36-60) is a frozen dataclass: ids, selected action, alternatives, evidence refs, policy version, score, proposed/approved/executed timestamps, execution_ref. Append-only JSONL/Postgres, `OutcomeLink` as separate events. No hash, no signature; the Postgres table has no immutability guard (only INSERT in code). Same class of gap.
- Approval tokens are HMAC-signed, bound to operator, capability, mission, node, `inputs_sha256`, exp and a nonce (approval.py:1-25). This is good, but replay protection is per-process (above).

Fix (C5, P1): add `prev_hash` and `entry_hash = sha256(prev_hash || canonical(event))` per mission stream at `append`; periodically sign a checkpoint head (Ed25519, key outside the host) and anchor it externally. Add `unknown` to the `ExecutionReceipt.outcome` set (runtime-contracts change). Make receipts reference `previous_receipt_id`.

### 6. Canonical transcript vs prompt projection, secrets in traces

- The harness design (agent-harness-dev branch `fix/context-correctness-observability`, commit 6578e89, agent_harness/trace.py) is correct in shape: canonical messages recorded at append time, before any manager rewrites them, content-addressed in `blobs/<sha16>.txt`; a per-call projection records hashes and lengths of what was shown versus canonical; full tool results and args are kept. It is opt-in and the recorder never fails the run (`_safe`).
- Problems: (a) no redaction anywhere in `TraceRecorder`; it writes full tool output, tool args, assistant text and the system prompt (`trace.add(0, messages[0])`), so any secret read by a tool or pasted by a user lands in plain files (default umask). `agentic_os/channels.py:32 redact()` and gate_notify's 0600 umask exist in agentic-os but are not applied to this trace. (b) `sha()` truncates to 16 hex chars (64 bits) and `blob()` skips writing if the file exists, so a collision or a pre-planted blob file silently substitutes content; there is no chain or signature on `trace.jsonl`. (c) Archives in context-runtime (`compression/transcript.py:131,210,221` produce `[archived A####: ...]` stubs) are stored in `archive_dir` with no redaction either (not deep-read).
- agentic-os mission path: `NodeSucceeded` stores the full result in the event log and `mission_receipt` returns it. Credentials are broker-mediated and destroyed after use (executor.py:`_provision`/`_release`), results can still echo secrets if an operator returns them. `SecurityMonitor` events are described as "already the redacted boundary facts" (security_monitor.py:128). `MissionTrace` is spans only.
- Fix (C6, P1): run `redact()` plus a secret-pattern and high-entropy scanner on every blob and projection before write; use full SHA-256; create the files 0600 in a 0700 directory; hash-chain `trace.jsonl`; document the trace as sensitive.

### 7. Placeholder-contamination guard (scenario 11)

- Located: /Users/arybach/projects/agent-harness-dev/agent_harness/context.py:26-47 (`_PLACEHOLDER_START`, `_PLACEHOLDER_ANYWHERE`, `placeholder_in`, `placeholder_arg`) and agent_harness/loop.py:151-158. Stub producers: context-runtime/context_runtime/compression/transcript.py:28,131,210,221, plus `ELIDED` and `chars omitted to save context` in context.py:22,87.
- Behaviour: before a tool call runs, every top-level string arg is checked; a hit returns "ERROR: argument 'X' is a context placeholder ... Nothing was run" as the tool result, emits `placeholder_blocked`, and counts a tool error. Also: PinSet keeps task instructions verbatim; archive-id redirect in read_file.
- Regression test: tests/test_context_correctness.py::test_an_action_carrying_a_placeholder_is_refused_not_run (reproduces the observed `write_file` with "[archived A0048: 17339 chars] ..." and asserts only the real write reached the tool), plus parametrized `test_placeholder_text_is_recognised` and `test_real_content_is_not_mistaken_for_a_placeholder`. CONFIRMED: 25 passed (run on a copy in the scratchpad; the dev dir was not touched).
- Gaps (CONFIRMED by running `placeholder_arg`):
  - Not merged: ~/projects/agent-harness (branch feat/ours-h2) has no `placeholder_arg`. The commit exists only on `fix/context-correctness-observability`.
  - Only top-level string args: `{'edits':[{'new':'[archived A0048: 9 chars] x'}]}` returns None. Nested dict/list args pass.
  - `_PLACEHOLDER_START` matches only at the string start (with leading whitespace). `"intro\n[archived A0048: 9 chars] x"` returns None; the case-changed `[Archived A0048` returns None. A model that prefixes real text then imitates a stub bypasses it.
  - Guard sits only in `run_loop`. Nothing equivalent in agentic-os (a model or tool result containing a stub could be written to a connector, e.g. via `personal_agent.connected_app_action` or an outreach send).
- Fix (P1): merge to main; scan args recursively (strings in dict/list); match the stub anywhere on a line start, case-insensitive; add the same check at the governed-operator boundary (reject text bound for an external send that matches the stub patterns); add tests for nested, mid-string and case variants.

### 8. Stale evidence, duplicate submissions, artifact format

- Stale evidence. Observations carry `known_at` and a provenance quality, and UNKNOWN fails closed for A0 evaluation (observation.py:28-53, observation_store.py:24,124, historical_replay.py:39-48). That protects retrospective learning. For execution: the Today digest binds recipients, content and attempt, and a changed digest returns 409 `stale_approval` (today.py:474-478; tests `test_wrong_or_stale_digest_is_refused_and_sends_nothing`, `test_mutated_content_invalidates_the_reviewed_digest`). The mission runtime's `ApprovalGranted` has no inputs hash and no expiry; the approval is looked up by node id and merged with `edit` (runtime.py:989-1007), and the HMAC token is minted over inputs resolved at dispatch (operators.py:67-69), so world state that changed between approval and execution is signed as approved. `GateExpired` is a notice-only marker (store.py:328-332). INFERRED, P1. Fix: record `inputs_sha256` and `world_epoch` in `ApprovalGranted`; at dispatch compare and re-park on mismatch; add an expiry that moves the gate to expired (blocked), not just a notice.
- Duplicate submissions. Today: per-item `phase in (sent, outcome)` replay returns the stored receipt (today.py:462-465), test_approve_is_idempotent_no_duplicate_send passes (CONFIRMED). Mission: parked-node check plus run lock (above). Webhook: `EventInbox` (integration/events.py:73-97) validates the HMAC and dedupes on (provider, event_id), tests/test_integration_phase1_events.py::test_inbox_validates_signature_appends_and_dedupes passes. EventInbox has no production caller outside tests and `_seen` is in memory only (CONFIRMED by grep); Telegram builds `interaction_id=tg:{chat}:{message_id}` (channels_telegram.py:122) but no dedupe store is shown. P1 for scenario 5: a duplicate webhook delivered after restart, or to a second replica, is processed twice.
- Artifact format. No check that a produced file matches the requested type (mime, extension, magic bytes, parseable). Only `projects/contracts.py:62` has a `mime_type` field (display) and tests/test_integrations_file_formats.py tests the file-provider read/write, not delivery verification. Scenario 13 is a gap (see below).

### 9. Scenario matrix

| # | Scenario | Existing test? | Status |
|---|----------|----------------|--------|
| 5 | duplicate webhook/retry => at most one side effect | Partially. tests/test_operator_sdk.py::test_operator_invoke_dedupes_on_idempotency_key, test_mission.py::test_executor_idempotency_dedupe, test_today.py::test_approve_is_idempotent_no_duplicate_send, test_integration_phase1_events.py::test_inbox_validates_signature_appends_and_dedupes, test_revenue_restart.py::test_drain_outbox_is_idempotent. All pass (CONFIRMED). All are single-process, no restart, no cache eviction. | Partial. Gap: P0/P1 across restart/replica. |
| 6 | ambiguous success => UNKNOWN + reconcile before retry | None. Closest: test_today.py::test_connector_failure_is_not_done_and_retry_needs_a_fresh_digest (clean FAILED, not ambiguous). Probe shows stuck item, no UNKNOWN. | GAP, P0 |
| 10 | model claims sent but provider has no record | None. The fake adapter has `false_success` (no artifact), covered by external verification tests; fabricated artifact ref passes (probe). | GAP, P0 for real adapters |
| 11 | placeholder imitation | Yes in agent-harness-dev: tests/test_context_correctness.py (25 pass). None in agentic-os; unmerged. | Covered on branch only; bypass cases untested |
| 12 | deadline approaching with no verified completion | reliability.py unit test only (test_reliability_order.py::test_transition_overdue); not wired to mission/Today. | GAP, P1 |
| 13 | wrong deliverable format | None. | GAP, P1 |

#### Test sketches

- S5 restart: build `Operator` with a counting handler; invoke key K; construct a new `Operator` (fresh `_seen`); invoke K again. Assert the handler count stays 1 when a durable idempotency store is configured. Today it is 2 (expected failure, documents C3). Add a Postgres-backed store variant behind a marker. Second test: write NodeDispatched to a JsonlEventStore, "crash", rehydrate, `run()`; assert the node is UNKNOWN/parked, not re-invoked.
- S6: `Today(adapter=TimeoutAfterCommit())` (see scratchpad/probe_c.py). Assert after the exception: item.state == "unknown", `approve` again raises 409 `reconcile_required`, `reconcile(item_id)` queries the adapter `lookup(idempotency_key)`; FOUND => sent/verified, ABSENT => failed with retry allowed; provider effects stay at 1 throughout. Mission variant: operator raising TimeoutError after side effect => NodeState UNKNOWN, no saga call, no MissionState.FAILED.
- S10: adapter returning `artifact_refs=("artifact:made-up",)` with no provider record; `Today.approve` must yield HELD/UNKNOWN, `delivery_verified False`, once a `verify_effect` probe returns ABSENT.
- S11: tests for `placeholder_arg` with nested args, mid-string stubs, `[Archived ...]`; an agentic-os operator test that an outbound send body matching the stub patterns is refused before dispatch.
- S12: mission with a node `deadline_at`; clock past `deadline_at - lead` with no VerificationRecorded => `ReliabilityIncident(OVERDUE)` event and an AttentionKind.AT_RISK item; past deadline => BLOCKED, not silently RUNNING. Same for a Today case with `deadline` set (today.py:81).
- S13: capability declares `output_format="xlsx"`; the operator returns a file that is CSV. Verifier must REJECT on magic bytes (zip + `[Content_Types].xml`) and parse. Parametrize xlsx, docx, pdf, csv, json with renamed-extension files.

### 10. What is working well (keep)

Fail-closed staging (`is_staged` + `staged_not_executed`, runtime.py:808-823); verifier denylist that never mints success from failure-shaped results (verify.py:21-30,100-130); single-use, request-bound approvals in Today and the external bridge (approval_bridge.py:107-120); compensation authorization; credential redemption only at the boundary with destroy and revoke; per-mission run lock; Today's separation of sent versus delivery_verified versus outcome_observed, and its stale-digest refusal.

### 11. Prioritised fix list

P0: C1 UNKNOWN state and reconcile-before-retry (mission, Today, receipts); C2 independent postcondition probe for side-effecting capabilities; C3 durable idempotency and a crash-window rule (NodeDispatched with no terminal => UNKNOWN), plus a cross-process mission lease.
P1: C4 retry/timeout policy tied to UNKNOWN; C5 hash-chained, signed event log and receipts; C6 trace redaction, full hashes, 0600; C7 merge and harden the placeholder guard, add a boundary check in agentic-os; C8 wire ExpectedTransition into mission nodes and Today; C9 artifact-format verification; C10 approval bound to inputs hash, world epoch and expiry; durable webhook dedupe with a production caller.
P2: expose per-node attempts/limits; cancellation fence for late results on the mission path; add a VERIFYING node state event for observability.

## Side-effect path inventory (workstream A, item 4)

Question per path: does it pass through ONE central policy-enforced dispatcher, or bypass it? There are **three** governed dispatchers, not one, and none is universal:
- **D1 AgentGateway.invoke** (`agentic_os/agent_gateway/gateway.py:126`): external-agent path.
- **D2 Mission Executor.run -> Operator /invoke** (`mission/executor.py:228`, `operator_sdk.py:253`): policy plane + signed approval token.
- **D3 agent_harness ToolRegistry.run** (`~/projects/agent-harness/agent_harness/tools.py:137`): tool approval policy.
"Gate" below = approval/policy check before the effect. Labels: C = CONFIRMED (code read; most rows read by delegated sub-audits, spot-checked by me for items marked C*), I = INFERRED.
Spot-checked by me (C*): content/app.py:19, projects/app.py:70 + content_service.py:150, safe_url zero callers, approvals.py:64, gateway.py defaults, apps/billing/core.py:486-520.

### A. BYPASSES of central enforcement (ranked)
| # | file:line | Function / route | External effect | Why it bypasses | Sev |
|---|---|---|---|---|---|
| B1 | `agentic_os/projects/app.py:70` -> `projects/content_service.py:131-160` (publish at :150) | `POST /api/missions/{mid}/decide` -> `ProjectsContentService.decide` | Posts to X / LinkedIn / IG / TikTok via `self._publisher.publish(draft)` | No auth on route; publishes directly, then calls `run.approve()` with a no-op publisher (:157) so the mission only records after the fact; `actor` is client-supplied (:70 body) | **Critical** C* |
| B2 | `agentic_os/content/app.py:19` | `POST /api/projects/{pid}/missions/{mid}/approve` | Resolves the Mission publish gate, then `MultiPublisher` posts to X/LinkedIn/IG (content/fleet.py:68-91, clients.py:215/289/317) | Gate is real but the **approval decision endpoint is unauthenticated** (anyone reaching it is the approver) | **High** C* |
| B3 | `deploy/admin-console/app.py:31` -> `world/seeder.py:47`, `world/adapters.py:117-258` | `GET /api/admin` | Creates Lago customers, Chatwoot contacts, Twenty companies, ERPNext Customer/Sales Invoice/Payment Entry/Expense/Journal Entry when core env vars set | Unauthenticated GET; `ExecutionMode.SIMULATE` default not consulted by `ProjectionSeeder.project` | High (conditional on env) C |
| B4 | `agentic_os/control_plane.py:355-370` | `/m/{name}/api/{path}` GET/POST proxy | Forwards arbitrary POST bodies to module services | No auth unless `DEMO_READ_ONLY=1`; downstream auth unknown. Same shape in context-runtime `control_plane/app.py:291` | High C / downstream I |
| B5 | `integrations/execution.py:149-171`, `agent_gateway/gateway.py:233-238` | `run_test_mission`, gateway `_envelope` -> adapter `execute(..., envelope)` | Real Slack, Gmail, Stripe, HubSpot, WhatsApp via `integrations/live.py:71-106`; Google Sheets/Docs, Graph, local files | `GovernedEnvelope` is self-minted and adapters only test `envelope is None` (google_app.py:226, microsoft_app.py:231, file_formats.py:307, libreoffice_app.py:223); it proves no approval | High (design) C |
| B6 | `interaction/channels_telegram.py:141-168`, `channels_whatsapp.py:123-162`, operator `interaction/operator.py:68` | `interaction.reply` (side_effecting) | Outbound chat/voice/file to customers | No `approval_required`; file comment "base stub does not gate" (operator.py:7). Reachable via any authenticated /invoke | Medium C |
| B7 | `agentic_os/channels.py:118-231,264` + `Gateway.serve_forever :319` | Telegram/Slack/Discord/SMS send; chat poller | Chat messages; poller dispatches to fleet | Direct; poller authorizes by spoofable username allowlist (:297), open if `GATEWAY_OPEN=1` | Medium C |
| B8 | `world/connectors.py:135-139` | `HubSpotSequenceSender.send` | CRM upsert then sequence enroll (email) | Writes CRM contact (:138) before checking `available()` (:139); `world/outreach.py:200` `REDEVOPS_AUTO_SEND=1` removes human review | Medium C |
| B9 | `integrations/outreach.py:174-176` | `run_outreach` | `provider.activate()` starts email sequence | `governance="allow"` is the default = auto-activate | Medium C |
| B10 | `revenue/loop.py:73`, `visualization/governed.py:59` | `execute_quote_plan`, `create_visualization` | ERPNext quotation/opportunity writes; Metabase card/dashboard | Approval is a caller-asserted `approved: bool`; `InMemoryVizApprovals.approve(by=)` unauthenticated (governed.py:44) | Medium C |
| B11 | `social/meta_graph.py:51,63`; `intelligence/email/bridge.py:98`; `collaboration/{slack,msteams,google_chat,whatsapp}.py` | publish FB/IG; send email; chat posts/edits | external posts / email | Library calls, token-presence only; no in-package caller mostly -> **latent** (reachable by any future caller w/o gate) | Low-Med C |
| B12 | `integration/providers/http.py:118-136`, `integration/obligations.py:68-95` | generic REST write; retry-until-discharge | ERP create/update | No gate; retries even when verification found conflicts | Medium C |
| B13 | `apps/edge-sentinel/operator.py:38,51` vs `core.py:290,375`; `apps/growth-assistant/core.py:577`; `apps/edge-sentinel/app.py:707-710` | `block_ip`/`unblock_ip`, `push_asset`, console `ban_ip` | CrowdSec decisions; Postiz/Listmonk/ERPNext pushes | Gate lives in the operator wrapper, `core.*` execution functions are ungated ("Caller gates"); console tool relies on harness staging (I) | Medium C |
| B14 | `apps/agentic-privacy/operator.py:46 -> core.py:733-763, 270-272` | `privacy.intake` | Emails a verification link to a caller-chosen address; link host taken from caller `base_url` when `PRIVACY_PUBLIC_URL` unset | No approval; phishing relay through company mail | Medium C |
| B15 | `apps/agentic-crm/operator.py:32-58`, `apps/social-autopilot core.py:294-345`, `apps/support core.py:295-345`, `apps/lifecycle core.py:145-161`, `apps/infra operator.py:113,132` | score_lead/research/draft_outreach; social.draft; support.draft_reply; compose_campaign; infra.plan/drift | ERPNext comment/PUT; Postiz DRAFT INSERT; Chatwoot private note; Listmonk draft; terraform plan / ansible --check | Ungated by design (draft/annotate/read-only tooling) but all external writes or process execution on any authenticated /invoke | Low-Med C/I |
| B16 | `agentic_os/projects_api.py:1635` (`Today.approve`, today.py:455) | `POST /api/projects/{pid}/today/{item}/approve` | `ExternalAgentOperator(adapter or FakeExternalAgentAdapter())` | Unauthenticated self-approval; safe only because default adapter is fake | Low now / High if real adapter injected C |
| B17 | `secrets/bootstrap.py:133-156`, `cli.py:61-71`, `secrets/stores.py:201` | rc-file edits (shell injection if path has `"`/`$()`), `secrets get` prints value, Vault URL no `..` sanitation | local config / secret disclosure | CLI/library, no gate | Low C |
| B18 | `control_plane.py:492,497`, `fleet.py:65-88` | `/up` `/down` | `git clone`, `docker compose up/down` | Gated by `require_api_key` only, no approval, argv lists (no shell) | Low (authn OK) C |
| B19 | `apps/video-ad/operator.py:78-111` | `video.render_segment`, `regenerate_shots`, `unpublish` | paid render, retract social post | Handlers in external `reel` package, not auditable here; `unpublish` not `approval_required` | Unknown I |

### B. Paths that DO go through a gate (central-ish)
| file:line | Effect | Gate | Dispatcher | Residual weakness |
|---|---|---|---|---|
| `apps/billing/core.py:504`, :306, :554, :618 | Lago refund credit note, dunning retry, subscription create/cancel | `_approval` truthiness inside core | D2 | bare truthy check; approvals re-trusted in-process (`Operator.invoke` passes `_approval` through, operator_sdk.py ~330) C |
| `apps/books/core.py:333,540,605,666,783` | ERPNext categorize/close/journal/reverse | same | D2 | same C |
| `apps/support/core.py:392,508,541` | Chatwoot customer message/resolve/assign | `gated()` + core | D2 | `conv_id` interpolated unvalidated into URL path (I) |
| `apps/compliance/core.py:413,551` | ticket POST; consent ledger append | same | D2 | consent idempotent via ledger scan (durable) C |
| `apps/infra/operator.py:55,123,135,139` | terraform apply/destroy, ansible, rollback | `_approval`/`gated()` | D2 | `plan_digest` optional: apply unbound to approved plan (operator.py:60-62) C |
| `apps/sky/operator.py:38,61,77,83` | cluster launch/serve/down | `_approval` | D2 | user `run` string executes on cluster (approved) |
| `apps/social-autopilot/core.py:482` | Postiz `UPDATE ... state='QUEUE'` (publish) | `_approval` + content-hash binding (:506-522) | D2 | unapproved staging call records the binding anyone can seed |
| `apps/document-editor/operator.py:108,130,144` | doc apply_patch/publish/label | `_approval`; restricted-label convert/export re-verify token (`classification.py:209`) | D2 | only app re-verifying a token in-handler |
| `apps/outreach-engine/core.py:360,376` | Twenty sync; send_all (no-op) | `_approval` | D2 | send_all approval covers whatever is queued at exec time |
| `agent_gateway/write_capabilities.py:49-68` | CRM outreach send, projects change | `CONSEQUENTIAL` -> REQUIRED approval, envelope | D1 | approval never consumed/expired (approvals.py:64); idempotency in-memory; **no live wiring of writes found in projects_api (read-only grants)** C |
| `content/fleet.py:36,68` | social publish via mission | `approval_required` | D2 | see B2 |
| Harness tools flagged `side_effecting` | any | `ApprovalPolicy.decide` (tools.py:73-85) | D3 | gate keys off the tool's *self-declared* `side_effecting` (default False, tools.py:28, :170) |

### C. Cross-cutting facts
- **safe_url.py has zero callers** (`public_url_problem`, safe_url.py:61; grep over repo excluding tests finds only the definition) -> no SSRF guard on `meta_graph image_url`, projects_api source `location`, Salesforce instance URL, WhatsApp media URL (token sent to attacker-influenced URL, channels_whatsapp.py:121), `integration/providers/http.py:57`. Only market-radar has its own `_watch_url_error` (registration-time, rebinding window). C*
- `approval_gate.gated()` (approval_gate.py:16) used by operators in apps/ (support, crm, infra) but not within agentic_os package; it is a bare `inp.get("_approval")`. C
- In-process path trust: `LocalOperatorClient`/`Operator.invoke` accept `_approval` as given; edge-sentinel/response.py:88 injects it from a Governance decision. Anything importing an Operator can forge approval. C
- No `shell=True` in agentic_os; argv lists everywhere (fleet.py:81,87; secrets/bootstrap.py:216; libreoffice_app.py:164). C
- Approvals shipped as `OPERATOR_BASIC_AUTH` static shared secret minting `_approval={"approved":True,"by":"operator-basic"}` (apps/*/core.py: crm 55-83, outreach ~85-97, support ~60-77, billing/app.py:334, books/app.py:372, compliance/app.py:346): no nonce, expiry or input binding. C
