# Threat model (workstream A)

Scope: agentic-os 0.3.42 + context-runtime 0.1.0 + agent-harness 0.1.0 + runtime-contracts 0.3.14. Status: CONFIRMED = code read and/or an existing targeted test passed; INFERRED = deduced from code/comments; UNTESTED = no test or code path verified. Severity is for this code as shipped (open-core defaults), not for an enterprise overlay that is not in the repo. Refs: `data-flow.md`, `side-effect-inventory.md`, `architecture-map.md` in this directory.

## 1. Trust boundaries
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

## 2. Threats
Columns: where in THIS code | existing mitigation (file:line) | status | severity.

### T1 Prompt injection (direct goal / indirect via retrieved or tool text)
- Where: planner prompt built by string replace of goal (`mission/planner.py:127`); LLM output parsed with greedy regex `\{.*\}` (:146) and only fills `outcome/need` strings. Operator results flow into world state and `$from_world` inputs of later nodes (`runtime.py:845-861`, `_resolve_inputs` :962-964). Harness loop feeds tool output straight back to the model (`agent_harness/loop.py:163`).
- Mitigation: model never picks tools/args in the Mission path (planner returns outcomes; binding by compiler/registry) -> blast radius limited to "which outcomes"; PolicyScopedRegistry; MANDATORY/regulatory approval for money actions (policy_decision.py:104,207); parse failure falls back to deterministic planner (planner.py:139). Harness: side-effecting tools gated by ApprovalPolicy (tools.py:73-85).
- Gaps: no content/instruction separation or injection scanning anywhere (grep none); in harness loop the model chooses tool name and args directly; approval gate keyed on tool's self-declared `side_effecting` (default False, tools.py:28).
- Status: CONFIRMED (code) for mitigations and gaps; actual injection success UNTESTED. Severity: Medium (Mission path) / High (harness agents with side-effect tools in `bypass` or `allowlist` mode).

### T2 Tool-output laundering (untrusted output becomes trusted state)
- Where: `_finish` accepts operator-reported `_observations` with the operator-chosen `confidence` (clamped 0..1), `source`, `source_type` (runtime.py:845-861); `world.observe(node.produces, result, confidence=1.0)` (:843). Downstream nodes consume via `$from_world`; belief fusion only gates on conflict/low confidence (`_belief_issue` runtime.py:1046).
- Mitigation: low-confidence/conflicting beliefs park for disambiguation (runtime.py:762/1084); reserved keys; `strip_approval_keys` on mission inputs (runtime.py:297).
- Gap: an operator (or a result that echoes attacker text, e.g. a CRM comment) can assert `confidence=1.0, source_type="human"`. Egress filter (gateway) regex-redacts only on the way out.
- Status: CONFIRMED (code read). Exploit UNTESTED. Severity: Medium.

### T3 Malicious delegation (subagent / delegated mission gets more than parent)
- Where: gateway MISSION capability `gateway.py:175-189` passes `arguments` (incl. `constraints`) to `MissionPort.delegate`; REST `POST /missions` accepts caller `constraints` and `policy_refs` (api.py:26-27,71-95).
- Mitigation: tenant callers cannot choose authority: grants from server config, wildcard rejected, budget ceiling (api.py:80-91, control_auth.py:101-127); constraints can only ADD approval gates; approvals never inherit across revision/capability (runtime.py:1783-1830). Authority chain leasing exists as optional Executor seam (executor.py:231-242).
- Gap: admin token default grants `*` (api.py:93); in-process `_pre_exec_ok` no-op (runtime.py:746); gateway `delegate` with a real MissionPort is not wired in repo.
- Status: CONFIRMED mitigations; delegation chain enforcement UNTESTED (enterprise seam absent). Severity: Medium.

### T4 Model switching after refusal
- Where: `router.py:83-100` falls up tiers only on transport exceptions (URLError/OSError/ValueError); a model *refusal* is a successful text response and is returned as-is (no retry on refusal); `GovernedModelRouter` has no refusal handling either. Agent-harness `llm.py` is a minimal client.
- Mitigation: none specific, none needed for refusal-retry because none exists; but tier fallback to a pricier/less-restricted model on transport failure is silent (no receipt of fallback in router.RouteResult beyond tier name). `InferenceReceipt` (governance/receipt.py) records endpoint when `GovernedLLM` is used.
- Status: INFERRED (no refusal-detection code found by reading router.py/routing.py; harness llm.py not fully read). Severity: Low.

### T5 Excessive inherited privileges
- Where: admin bearer = all tenants, wildcard grants (api.py:93; control_auth.py:162); the runtime uses the same admin bearer to call every operator (operators.py:77-80) so any operator accepts any capability (no per-capability authz, INFERRED); `PolicyDecisionPlane.permissioned` true when `"*"` in grants (policy_decision.py:80); `_pre_exec_ok` default True; gateway `_AllowAllBudget`/`_AllowAllEgress`/`IF_POLICY` auto-run defaults (gateway.py:57-80,228-231); in-process approvals trusted.
- Mitigation: tenant tokens scoped; approver tokens distinct and cannot invoke (control_auth.py:186); capability visibility = scopes AND permissions (registry.py:78-84); gateway write capabilities approval REQUIRED; tests pass (`test_agent_gateway*.py`).
- Status: CONFIRMED. Severity: **High** (open-core defaults are permissive by design; production relies on overlay).

### T6 Credential exfiltration
- Where: approval HMAC secret and all platform secrets in plaintext `secrets.env` (secrets/bootstrap.py:80-105) and `agentic-os secrets get` prints values (cli.py:61-71); shell rc edits with unquoted path (bootstrap.py:114,133); Vault URL not sanitised (stores.py:201); token sent to URL taken from provider response (channels_whatsapp.py:121); token in URL for Telegram (channels.py:122); tool outputs fed back to model (harness loop.py:163) could carry secrets; `receipt reason=str(e)` may include URLs (intelligence/email/_base.py:81).
- Mitigation: credential broker redeems only at executor boundary and destroys material in `finally` (executor.py:169-226,277-280), secrets never in `inputs`/log (executor.py:26-28, receipt.py docstring); gateway egress DENY for `SECRET` and regex redaction (egress.py:25-39,101-110); channel errors redacted (channels.py:32,85); file secrets 0600 atomic.
- Gap: broker implementation absent from repo; egress is regex/key-name based (misses e.g. short or novel secret shapes); default egress allows everything (gateway.py:57).
- Status: CONFIRMED for mitigations/gaps; broker behaviour UNTESTED here. Severity: Medium-High.

### T7 Replay / duplicate actions
- Where: gateway approval not consumed (approvals.py:64) -> identical request re-executes forever after one approval; gateway idempotency store in-memory (gateway.py:101-105) and checked before authz (gateway.py:130-136; key is principal-scoped so low risk); Operator idempotency & `ReplayGuard` per-process (operator_sdk.py:139-142; approval.py:180-204; `SharedStore` protocol has no implementation :152-163, replicas only warn :166-177); nonce consumed before handler runs so a lost response yields staged retry (not duplicate) (approval.py:145); app-level dedupe only Lago `external_id`, consent ledger, Postiz PUBLISHED check; no key supplied -> no dedupe.
- Mitigation: token binds inputs hash/mission/node/exp 300 s/nonce (approval.py:102-149), tests `test_approval_token.py` pass; compensation idempotent (executor.py:344-348); in-flight claim prevents concurrent duplicates in one process.
- Status: CONFIRMED. Severity: **High** for refunds/dunning/publish/`terraform apply` on restart or >1 replica; Medium otherwise.

### T8 Spoofed receipts / audit tampering
- Where: `mission_receipt` folds an unsigned, un-hash-chained JSONL (receipt.py:18-47; store.py:124-155); anyone with file/DB write can rewrite history; verifier trusts operator-returned dict (verify.py:177-217); gateway `AuditEvent` in-memory; `GovernedEnvelope` self-minted (integrations/execution.py:169); approvals in receipt lack approver identity and input hash (receipt.py:35-37); chat `Context.resolve` rewrites approvals.jsonl.
- Mitigation: append-only API, fsync, flock (store.py:124-150); events pin policy digest/plan fingerprint (runtime.py:698,466); Postgres backend DB-assigned seq (event_backends.py:128); `executor.monitor.observe` at the boundary independent of operator (executor.py:281).
- Status: CONFIRMED. Severity: **High** for financial/legal non-repudiation.

### T9 Confused deputy
- Where: runtime holds one bearer that every operator trusts (operators.py:77-80); app Basic-auth approver creds mint `_approval` for any input (apps/*/core.py); `intelligence`/`projects` routes act with server credentials for unauthenticated callers (B1-B4, B16); privacy.intake sends company email to caller-chosen address with caller-chosen link host (core.py:270-272, 733); `/m/{name}/api` proxy forwards with server identity (control_plane.py:355).
- Mitigation: tenant pinned onto inputs (`_tenant`, operator_sdk.py:260-266, :280); tenant mismatch 403; approver cannot invoke.
- Status: CONFIRMED. Severity: High (B1/B2), Medium others.

### T10 Cross-tenant leaks
- Where: mission API 404 cross-tenant (api.py:49-52) and `list_missions` filter (api.py:97); `control_plane /approvals`, `/jobs/{id}` unauthenticated list approval payloads (control_plane.py:502,624); `/missions/{mid}/events` is tenant-checked; execution context single-tenant default (execution_context.py:1-20); in-memory idempotency keys omit tenant in Operator (key = capability, inputs, key; tenant only via `_tenant` input, INFERRED); apps use process-global state (outreach `_STATE["approved"]`).
- Mitigation: tenant tokens, `_authorize_mission`; gateway idempotency key includes tenant/subject (gateway.py:133).
- Status: CONFIRMED for mission API; app-level isolation UNTESTED. Severity: Medium (single-tenant default) / High if multi-tenant deployed on open core.

### T11 Poisoned retrieval
- Where: retrieval is external (context-runtime, redevops-rag; `sources_rag.py`); retrieved text enters model prompts (harness loop; context-runtime plans) with no provenance-trust levels beyond `derived_from`; world observations take operator-asserted confidence (T2).
- Mitigation: context-runtime design principle "compression keeps derived_from" (ARCHITECTURE.md §1, not code-verified); `VerifiedIntent` sealing pins evidence refs (runtime.py:128-150, epoch pins :464); Postgres source forced read-only (sources_postgres.py:107-125).
- Status: INFERRED / UNTESTED (retrieval code in other repos not read). Severity: Medium.

### T12 Compromised MCP server / third-party tool server
- Where: inbound: gateway MCP bridge validates bearer only, tool args not schema-validated against `input_schema` (gateway.py invoke never validates; mcp.py:50-56); outbound: context-runtime `tools/mcp.py` and `web_search.py` (not read); connector adapters from external package; Vault/HTTP providers accept any base URL, no SSRF guard (safe_url.py unused).
- Mitigation: gateway returns only `client_view` (contracts.py:215-228), hidden capabilities indistinguishable from unknown (gateway.py:141-143); egress policy on outputs.
- Status: CONFIRMED (gateway side), INFERRED/UNTESTED (outbound MCP client). Severity: Medium-High.

### T13 Denial of wallet
- Where: harness loop caps only turns (200) and wall clock (3600 s) (loop.py:55-56), no $ cap in loop (spend_proxy separate); `router.py:95-98` checks budget AFTER the paid call, counts output tokens only, in-memory `spent_usd` reset on restart; gateway `_AllowAllBudget` (gateway.py:78); mission `Budget` is projected at plan time (runtime.py:482) and not metered per node in open core (`_meter` no-op runtime.py:770); `POST /dispatch` background jobs unbounded; unauthenticated `intelligence` quote routes call paid providers (intelligence/api.py:65,76); video render usd 0.50 per call ungated (apps/video-ad).
- Mitigation: tenant budget ceiling (api.py:88-91); fail-closed price table in `agent_harness/spend.py` (`SpendError` unpriced model) used by `spend_proxy`; mission concurrency caps.
- Status: CONFIRMED. Severity: Medium-High.

### T14 Irreversible financial actions
- Where: billing.refund / dunning / subscription cancel (apps/billing/core.py:486-520,306,618), books close/reverse, `infra.provision` terraform apply/destroy; Stripe adapter reachable through `integrations/live.py:71-106` with a presence-only envelope (B5); `revenue/loop.py:73` boolean approval.
- Mitigation (strong, CONFIRMED): approval MANDATORY by regulatory markers (policy_decision.py:27,207); signed single-use token bound to exact inputs; gate precedes execution in core; `refund` requires explicit invoice and amount (core.py:488-499); staged results never commit (runtime.py:816-828); compensation requires evidence + registered undo + own approval (executor.py:309-375); `MissionPolicy` DENY on `irreversible` (policy.py:49-76, test run). Tests passed.
- Gaps: self-approval unless approver tokens set (api.py:127); approver sees no concrete args (policy_decision.py:172-185); replay across restarts (T7); `infra.provision` plan_digest optional.
- Status: CONFIRMED. Severity: **High** (residual).

## 3. Top 5 (by risk)
1. B1/B2 unauthenticated publish/approve on content & projects servers (T9, T14 adjacent) -- Critical/High.
2. Approval/idempotency/replay state is per-process; gateway approval never consumed (T7) -- High.
3. Open-core defaults are permissive: `_pre_exec_ok` no-op, admin `*` grants, allow-all egress/budget, in-process approvals trusted, self-approval default (T5) -- High.
4. Receipts/event log unsigned and unchained; verifier self-attesting (T8) -- High.
5. `GovernedEnvelope` is a presence flag and `safe_url` has zero callers (T6/T12/B5) -- High.

## 4. Not covered / follow-ups
Enterprise overlay (authority gate, broker, sandbox, metering) absent -> all "overlay will tighten" claims UNTESTED. Not read: context-runtime tools/mcp.py, retrieval/store plugins, agent_harness llm/egress_proxy/spend_proxy, runtime-contracts internals, Go port, `agent_gateway/oauth.py` in depth, `reel` package.
