# App boundaries audit (workstream D) — 2026-10-23

Scope: Edge Sentinel, Compliance, Privacy, Revenue/Finance, Guide, tenant isolation (scenario 8), evidence freshness (scenario 9).
Method: static read of source + test names. No tests were executed, no docker, no source modified.
Labels: CONFIRMED = read in code (file:line). INFERRED = follows from code but needs a live system to prove. UNTESTED = no test found.
Severity: CRITICAL / HIGH / MEDIUM / LOW.

Paths are relative to `/Users/arybach/agentic-os`.

---

## 1. Edge Sentinel (`apps/edge-sentinel`)

### 1.1 What it is
A CrowdSec wrapper (decisions via LAPI, alerts via `docker exec cscli`) plus a typed evidence/case spine (`evidence.py`, `case_store.py`, `incident.py`), STIX/ATT&CK/CTI, hunt, detection-engineering, DFIR, and governed response. Its own ARCHITECTURE.md is honest that "agent-security monitoring" is NOT yet delivered (`apps/edge-sentinel/ARCHITECTURE.md`, "NOT yet delivered" paragraph). The code agrees with that.

### 1.2 Telemetry that exists
| Source | Evidence | Real or sample |
|---|---|---|
| CrowdSec decisions (LAPI) and alerts (cscli) | `core.py:86 fetch_decisions`, `core.py:102 fetch_alerts` | Real when CrowdSec reachable |
| NetBird peers/policies | `netbird.py` header | SAMPLE data until `NETBIRD_API_TOKEN` set (labelled) |
| Portabase backup coverage | `portabase.py` header | SAMPLE until token set (labelled) |
| deploy-scan (nmap/nuclei/ZAP) proxy | `deploy_scan.py` header | Real only if `DEPLOY_SCAN_MCP_URL` set |
| Runtime events (agent telemetry) | `ai_security.py:51 observation_from_runtime_event` | Library only — see 1.5 |

### 1.3 Coverage gaps
- E1 (CONFIRMED, MEDIUM) `fetch_alerts` returns `[]` on any non-zero rc or empty stdout (`core.py:102-111`). "CrowdSec CLI failed / docker socket missing / sudo prompt" is indistinguishable from "no alerts". Fix: return `(alerts, status)` and surface a "telemetry unavailable" state in KPIs, and raise a self-alert after N consecutive empty-with-error polls.
- E2 (CONFIRMED, MEDIUM) Alert transport depends on `sudo docker exec` (`core.py:48-64`). Inside the container it needs the docker socket mounted (root-equivalent on host). Fix: use LAPI machine-login for alerts and drop the socket mount.
- E3 (CONFIRMED, LOW) Only host/network perimeter telemetry. No sensors for apps in this repo (billing/books/etc.), no auth-failure feeds from the Mission API, no gate-notification or approval anomalies.

### 1.4 Alert routing
CONFIRMED (HIGH): there is no outbound alert routing. `grep -i "notify|webhook|slack|pager|email"` across `apps/edge-sentinel/*.py` (non-test) finds nothing but UI strings. Alerts are a dashboard feed (`app.py` `_alert_feed`) and a case store. The only paging-like mechanism in the repo is the Mission Runtime `GateNotifier` (`agentic_os/mission/gate_notify.py`), which notifies on approval gates, not on security findings. Fix: route SecurityCase creation (severity >= HIGH) through GateNotifier-style outbox with dedupe key = case id.

### 1.5 Can it pause or revoke runs?
CONFIRMED (HIGH): No. Edge Sentinel's operator exposes exactly three capabilities: `sentinel.triage`, `sentinel.block_ip`, `sentinel.unblock_ip` (`operator.py:26-59`). All side effects are CrowdSec IP bans. It imports only `operator_sdk`, never `MissionRuntime`.
- The runtime does have `MissionRuntime.suspend()` (`agentic_os/mission/runtime.py:1364`) and `cancel_mission` (`runtime.py:371`) and a credential broker with revoke (`agentic_os/secrets/stores.py:6`, `agentic_os/provisioning/identity.py`). Only `agent_gateway/mission_adapter.py:75` calls `suspend`. No HTTP route in `mission/api.py` exposes suspend, and nothing connects a security finding to either.
- `ai_security.py` docstring says it "cannot (and does not) re-drive the denied action"; it is an observer by design (`ai_security.py:12-16`).
Fix: add a governed `sentinel.suspend_mission` / `sentinel.revoke_grant` capability bound to `MissionRuntime.suspend` and the secret store `revoke`, approval-gated, with verification by read-back of mission state.

### 1.6 Agent-assurance sensors (the requested list)
Source of truth: `apps/edge-sentinel/ai_security.py` (134 lines) and `agentic_os/mission/security_monitor.py` (opt-in runtime boundary monitor).

| Sensor | Edge Sentinel | Evidence |
|---|---|---|
| Unusual tool calls | NOT_PRESENT | `assess_ai_security` (`ai_security.py:79-113`) has 3 rules only: injection markers, action after DENIED, DENIED capability. No baseline, no rarity, no sequence/novelty score. |
| Privilege attempts | PARTIAL | Rule 2 (invocation after policy DENIED, `ai_security.py:~95`) and rule 3 (capability DENIED) are privilege-denial proxies. No detection of requesting a permission outside the plan. The runtime-side `SecurityMonitor.disposition` flags plan-vs-observed divergence (`security_monitor.py:127`), but it is opt-in and is not consumed by Edge Sentinel. |
| Model switching | NOT_PRESENT | No field for model id in `security_projection` (`ai_security.py:49-58`). `grep model.?switch|model_changed` in `agentic_os/mission` finds nothing. |
| Repeated failures | NOT_PRESENT | No counter/window anywhere in `ai_security.py`. Runtime has `fleet_slos` failure rates (`mission/observability.py:61`) but not as a security signal. |
| Stale credentials | NOT_PRESENT | No credential-age or rotation check. Runtime emits credential issued/redeemed/revoked/denied events (`security_monitor.py:84 observe_credential`) with no age analysis. |
| Missing receipts | NOT_PRESENT | No check that a side-effecting invocation has a matching receipt/verification. Edge Sentinel's own `execute_response`/`verify_response` (`response.py:76,140`) shows the pattern but it is not applied to other apps. |
| Deadline breaches | NOT_PRESENT | `Obligation` engine detects `OBLIGATION_OVERDUE` (`agentic_os/integration/obligations.py:97`) but Edge Sentinel does not subscribe to it. Mission `deadline` exists in `mission/types.py:291` only for scheduling. |
| Prompt injection markers | IMPLEMENTED (weak) | Substring list `ai_security.py:36`; trivial to evade ("leak" and "override" also produce false positives on benign payloads). |

Wiring: CONFIRMED (HIGH) `open_ai_security_case`, `ingest_runtime_events`, `assess_ai_security` are called ONLY from `apps/edge-sentinel/test_ai_security.py`. No route, poller, or runtime subscriber feeds real events in (grep across repo). So agent assurance in Edge Sentinel is test-only.
Tests: `test_ai_security.py::test_runtime_event_becomes_evidence_backed_observation`, `::test_induced_unsafe_sequence_opens_a_normal_investigation`, `::test_governance_held_the_denied_action_did_not_complete`, `::test_clean_sequence_opens_a_case_with_no_findings`, `::test_consumes_the_real_runtime_event_contract`.
Fix: (a) subscribe to `EventStore` (mission events incl. `RuntimeSecurityObserved`, `agentic_os/mission/security_monitor.py:122 durable_sink`) with a poller; (b) add the missing detectors as pure functions with frozen-corpus tests (per Phase 10 pattern in `evaluation.py`).

### 1.7 Evidence quality in the governed response path (positive)
- CONFIRMED: `propose_response` refuses side-effecting capabilities that are not approval_required (`response.py:61-73`); `execute_response` refuses without an approved decision (`response.py:76-85`); receipt and verification are separate and verification does parsed-IP equality (`response.py:111-137`). Tests: `test_response.py`, `test_block_guard_and_approval_state.py`.
- Residual (INFERRED, MEDIUM): approval authority is a single shared Basic credential per deploy (see 2.3), so "SecurityDecision.actor" is not a person.

### 1.8 Risk handoff to Compliance
CONFIRMED (MEDIUM): `risk_bridge.py:9-12` states there is no Compliance receiver. `RiskHandoff` is produced and goes nowhere. Compliance app (`apps/compliance`) has no endpoint accepting it.

---

## 2. Compliance (`apps/compliance`, `agentic_os/compliance`)

### 2.1 What it is
CONFIRMED: an OpenSCAP/CIS-Ubuntu scan viewer plus a hash-chained consent ledger, plus 4 hard-coded "SME items" (licenses/insurance). README markets "Vanta/Drata style" and "audit-ready evidence" (README line ~14; Guide card `apps/guide/core.py:28`). Product capability is far narrower than that wording.

### 2.2 Policy versions
NOT_PRESENT (CONFIRMED, HIGH). There is no policy/control document model, no version, no effective-date, no mapping of controls to frameworks other than the single scanned profile (`core.py:90 FRAMEWORK`). Only SSG release/hash of the scanned datastream is recorded (`core.py:77-82 _ssg_verification`; test `test_scan_records_which_ssg_hash_was_verified`). Mission-level policy changes are logged by the runtime (`PolicyChanged` events consumed by `agentic_os/compliance/evidence_producers.py:_GOVERNANCE_TYPES`), but the app does not surface them.
Fix: add `PolicyVersion{id, version, effective_from, owner, content_hash, supersedes}` to the ledger and bind each scan/approval to the version in force.

### 2.3 Approval authority
CONFIRMED (HIGH): approval is "request carries valid operator HTTP Basic credentials" (`gate.py:19-47`). `OPERATOR_MARKER = {"approved": True, "by": "operator-basic"}` (`gate.py:19`) — every approval is attributed to the same literal actor. No per-person identity, no role, no separation of duties, no delegation, no expiry. Same pattern in Edge Sentinel/Privacy/Billing/Books/Support.
Mission API does better: `api.py:124-139` rejects approval from a requester credential when `AGENTIC_OS_APPROVER_TOKENS` is set (separation of duty), records `actor=role:tenant`; test `tests/test_mission_api_default_deny.py::test_approver_tokens_block_requester_and_allow_approver`. But when approver tokens are not configured the requester can approve (`::test_requester_can_approve_when_no_approver_tokens_configured`) — CONFIRMED MEDIUM; and the app-level routes bypass that path entirely.
Forgery resistance (positive): `test_agent_run_forgery.py::test_agent_run_remediate_stays_staged`, `test_route_gating.py::test_forged_approval_in_body_is_still_staged`.
Fix: make apps accept the signed approval token from `docs/APPROVAL_TOKENS.md` with `sub` = person; fail closed when only a shared Basic credential is configured for CONSEQUENTIAL actions.

### 2.4 Evidence retention
NOT_PRESENT (CONFIRMED, MEDIUM). The consent ledger is append-only JSONL with no retention, rotation, archival or legal-hold concept (`core.py:50-52, 500-512`). `results/scan-results.xml` is overwritten per scan (single file, `core.py` SCAP_RESULTS), so scan history is lost. Positive: tamper-evidence works — `core.py:515 verify_chain`; tests `test_ledger_chain_verifies_and_detects_tampering`, `test_ledger_concurrent_appends_keep_chain_valid`.
Gap: chain verification only checks internal consistency; the head hash is not anchored anywhere external, so deleting the whole tail (truncate) is undetectable (INFERRED, MEDIUM; `verify_chain` has no expected-head parameter).
Fix: periodically publish head hash to an external anchor (e.g. event store + off-box), keep dated scan archives.

### 2.5 Audit export
NOT_PRESENT in the app (CONFIRMED, MEDIUM): routes are only `/health`, `/api/agent`, `/api/activity`, `/`, `/agent/run` (`app.py:469-497`) plus `/report` HTML. No export of ledger, scan history or approvals. A library projection exists — `agentic_os/compliance/evidence_producers.py` turns runtime events into `ControlEvidence` with ENFORCED/EVIDENCED discipline (tests `tests/test_compliance_evidence_producers.py::test_no_events_produces_nothing_not_a_false_pass`, `::test_hitl_approval_evidenced_only_when_a_human_acted`) — but nothing in `apps/compliance` calls it (grep: only `agentic_os/compliance/__init__.py`).

### 2.6 Hard-coded demo data presented as live
CONFIRMED (HIGH): `SME_ITEMS` constants (`core.py:95-100`) feed the "expiring" queue. The E&O policy expiry is `2026-09-08`, already past on 2026-10-23, yet `_sme_rows` only computes `days <= 90` -> label EXPIRING (`core.py:193-196`); a negative `days` is never labelled EXPIRED/LAPSED. Fix: add EXPIRED state, move items to a real store/connector.

---

## 3. Privacy / DSAR (`apps/agentic-privacy`)

Strongest app in this scope. 33+ named tests in `test_dsar_hardening.py`.

### 3.1 What works (CONFIRMED)
- Intake opens a PENDING DSAR, unique id with random suffix (`core.py:137-141`), `due_at = now + SLA_DAYS (30)` (`core.py:59, 748`).
- Single-use verification token, expiry, POST-consume vs GET-no-consume: tests `test_verify_wrong_token_replay_and_expiry`, `test_get_verify_does_not_consume_token_post_does`.
- Access is emailed, not returned: `test_access_after_verify_is_emailed_not_returned`, `test_access_export_token_is_single_use_and_expires`.
- Erasure stages unless verified+matching+approved: `test_delete_stages_unless_verified_matching_and_approved`; audit failure blocks erasure: `test_audit_failure_blocks_erasure`; partial erasure is reported as partial: `test_residual_yields_partial_erasure_and_honest_outputs`.
- Hash-chained audit log: `core.py:203 verify_audit_chain`.
- Rate limits and mail-bomb cap: `test_ip_token_bucket_429_with_retry_after`, `test_per_email_intake_cap_stops_mail_bombing`.
- LLM sees counts only: `test_llm_receives_counts_and_categories_only`.

### 3.2 Gaps
- P1 (CONFIRMED, HIGH) Lago subject lookup reads only the first 100 customers: `lago_find` uses `?per_page=100` with no pagination (`core.py:433-447`). A subject beyond customer #100 is silently "not found", so access/erasure reports can be wrong while looking clean. Fix: paginate (`_get_all` pattern in `apps/billing/core.py:57`) and filter server-side by `external_id`/email.
- P2 (CONFIRMED, MEDIUM) Over-claim in billing erasure: the note says "PII anonymized on request" (`core.py:461`) but `lago_delete` performs no anonymization call; it returns `deleted: 0, retained: N`. Fix: either call Lago customer update to redact PII fields, or change the note to "retained, not anonymized".
- P3 (CONFIRMED, MEDIUM) SLA is displayed, never enforced: `overdue` is a count in KPIs (`core.py:1183-1198`); no escalation, reminder or breach event. Fix: emit an overdue event/obligation at `due_at - 5d` and `due_at`.
- P4 (CONFIRMED, MEDIUM) Retention scan covers one system and one object type: ERPNext Leads not Converted (`core.py:1105-1120`). Lago/Chatwoot/Listmonk/S3 are not scanned. Purge swallows per-record delete errors (`except Exception: pass`, `core.py:1117`) and logs `purged_total` only, so failures are not itemized. Hard delete of "uncontacted leads" has no legal-hold check.
- P5 (INFERRED, MEDIUM) No tenant concept in the app (grep `tenant` finds only display strings); one DSAR store per deployment, so multi-tenant use needs a deployment per tenant.
- P6 (CONFIRMED, MEDIUM) Approval authority same shared Basic credential as 2.3.
- P7 (CONFIRMED, LOW) No DSAR extension/exemption states (GDPR Art. 12(3) extension, manifestly unfounded) — states are PENDING/verified/fulfilled only.

---

## 4. Revenue / Finance integrations

Detailed per-capability evidence is in `audit/business-admin-catalog.md`. This section is the boundary and semantics view.

### 4.1 Which system is the source of truth
| Object | Where it lives | Rule in code | Gap |
|---|---|---|---|
| Customers/subscriptions/invoices (SaaS) | Lago | Billing reads invoices and `payment_status` from Lago (`apps/billing/core.py:86-100`) | Display state collapses status/payment/overdue into one label; no explicit state machine |
| GL, AR/AP, bank txns | ERPNext | Books reads GL, Sales/Purchase Invoice, Bank Transaction (`apps/books/core.py:205-250`) | Books is single-company via env `COMPANY` (`core.py:43`) |
| Pipeline/contacts | Twenty / ERPNext CRM | `agentic_os/world/adapters.py:159-224` | Upsert by title only |
| Conversations | Chatwoot | `apps/support/core.py`, `agentic_os/integrations/chatwoot.py:83` | |
| Metric definitions | `SemanticRegistry` | five revenue metrics deliberately distinct, no single "truth" (`agentic_os/integration/semantics.py:100-121`) | Source priorities name stripe/quickbooks/salesforce which have NO adapters here (`connector_capabilities.yaml:116-128` lists quickbooks/xero/salesforce as UNSUPPORTED) |

No document anywhere declares a cross-system source-of-truth rule for the same object (e.g. invoice in Lago vs Sales Invoice in ERPNext). The two systems are never joined: no code links a Lago invoice to an ERPNext Sales Invoice or Payment Entry. CONFIRMED (HIGH).

### 4.2 Integration semantics (positive core)
- Obligation engine: write, then independently read back, classify satisfied / unsatisfied / conflicting / overdue (`agentic_os/integration/obligations.py:68-129`; tests `tests/test_integration_obligation.py`). Receipt is only issued on observed state.
- Connector capability registry fails closed: only VERIFIED is `usable` (`business/capabilities.py:21-35`). Statement "verification: contract" means fixtures, not live accounts (`connector_capabilities.yaml:14-17`).
- Gap (CONFIRMED, MEDIUM): `obligations.py:97` compares deadline and now as strings (`now > obligation.deadline`); correct only if both are same-format ISO UTC; also the overdue check happens only after retries are exhausted, so an obligation can be reported UNSATISFIED rather than OVERDUE depending on order. Fix: parse to datetimes and test deadline first.

### 4.3 Invoice/payment matching
- `books.reconcile` matches the FIRST open A/R invoice to the FIRST deposit with identical cents (`apps/books/core.py:379-400`). No customer, reference, currency, date, or one-to-many/partial-payment logic. Result string says "staged for posting" but there is no posting capability, so nothing is written. CONFIRMED HIGH. Test: `apps/books/test_contracts.py::test_reconcile_compares_to_the_cent`, `apps/books/test_operator.py::test_reconcile_matches_deposit_to_invoice` (assert the proposal only).
- Payout reconciliation (`agentic_os/integration/reconciliation.py:47`) enforces `charges - refunds - fees - disputes + adjustments == payout == deposit` with explicit tolerance and unknown-type surfacing. It is Stripe-balance-transaction shaped and lives under `integration/experiments/payout_reconciliation.py`; not wired to any app. Tests `tests/test_experiment_payout_reconciliation.py` (7 tests).
- Categorize is a tag, not accounting: sets `reference_number` and hard-codes category by direction ("Revenue · Sales" for deposits, "Materials · COGS" for withdrawals) (`apps/books/core.py:325`). Result is honestly labelled `mode: tag_only`. CONFIRMED MEDIUM (name overstates).
- Close gate vs agent capability mismatch (INFERRED, MEDIUM): the close checklist requires "Bank transactions reconciled" (`core.py:253-265`), but no agent action reconciles; so an agent-only loop can never reach a ready close. Fix: add a governed Payment Entry / Bank Transaction reconcile capability, or drop it from agent-owned blockers.

### 4.4 Approval gates and receipts
- Money-moving actions are gated in core, not in the route: `chase_invoices` (`billing/core.py:289`), `stage_refund` (`:486`), `create_subscription` (`:543`), `books.close`, `record_revenue`. Tests: `apps/billing/test_refund_and_forgery.py`, `test_route_gating.py`, `apps/books/test_route_gating.py`.
- Verified outcomes (positive): dunning reads each invoice back (`billing/core.py:340-356`), `partial` is never `done` (tests `test_closed_loop.py::test_partial_settlement_fails_the_node_and_is_reported_partial`); close reads back the PCV and GL (`books/core.py:484-505`; test `test_close_executes_verifies_idempotent_and_rejects_inconsistent`).
- NOT verified (CONFIRMED): refund reports success from the POST response, no read-back of the credit note (`billing/core.py:486-527`); `create_subscription` same (`:543-606`).
- Legacy dunning path always returns `status: "done"` regardless of Lago HTTP result and its summary says "dunning reminder sent" (`billing/core.py:201-258`, esp. 248-252). Dashboard button `_chase_overdue` uses this path (`billing/app.py:344-346`). HIGH: false-success on the human-facing surface. Fix: route the dashboard to `chase_invoices` or apply the same read-back and status logic.
- Refund double-spend guard (INFERRED, MEDIUM, UNTESTED): refundable = sum of positive fee `amount_cents` (`billing/core.py:461`), without subtracting existing credit notes; relies on Lago to reject over-credit. Fix: read existing credit notes and cap.

### 4.5 CRM / Chatwoot / Lago read paths
- ERPNext open receivables: hard `limit=200` with no order (`integrations/erpnext.py:109`), so AR above 200 invoices is silently truncated; currency not carried (rows only hold cents) (`:127-133`). CONFIRMED MEDIUM. Mixed-currency materiality sums amount x days across currencies (`business/missions.py:124`) CONFIRMED MEDIUM.
- Receivable cases hard-code `delivery_state=DELIVERED` and omit dispute/support state (`commercial/receivables_sources.py:65`), so from live ERPNext every overdue invoice resolves to COLLECT (test `test_erpnext_receivables_source.py::test_genuinely_overdue_case_resolves_to_collect`). The "reason across delivery/dispute/support" claim in `receivables.py` docstring is true only for the pure function, not for the live feed. CONFIRMED HIGH.
- Lago expansion scan: `usage_ratio` = current usage / plan recurring amount; healthy-account gate defaults to True (`integrations/lago.py:70-100`). Tests `tests/test_lago_expansion_source.py`.

### 4.6 LLM and money
CONFIRMED: no code path lets an LLM compute tax, totals, refunds or amounts. LLM calls in billing/books/support are narration only (`billing/core.py:226 blurb`, `books/core.py:353`), routed through `governed_text`. The model planner emits outcomes and needs only (`mission/planner.py:107-139`), no numeric inputs. Quote totals are deterministic integer math (`revenue/quote.py:85-153`). Residual (INFERRED, LOW): LLM narration can quote wrong figures to an approver; the figures are not machine-checked against the staged proposal. Fix: render the approver summary from structured fields, show LLM text as clearly secondary.

### 4.7 State machines (see catalog for detail)
Neither state machine exists. CONFIRMED (HIGH): no invoice lifecycle DRAFT->VALIDATED->APPROVED->ISSUED->DELIVERED->PAID, no tax-obligation lifecycle OBSERVED->EVIDENCE_REQUIRED->REVIEW_REQUIRED->SUBMITTED->ACCEPTED. `grep` for TRANSITIONS/StateMachine/can_transition in `agentic_os` and `apps` returns nothing. What exists: a free-text `Invoice.status` comment vocabulary (`business/contracts.py:149`), a display collapse (`billing/core.py:86`), order stage chain monitored for missed transitions (`commercial/order.py:17,42-61`), collection stages (`commercial/receivables.py:90-100`). These monitor, they do not enforce.

---

## 5. Guide (`apps/guide`)

### 5.1 What it is
CONFIRMED: an onboarding chatbot over 14 hard-coded app cards (`core.py:21`), NOT a document knowledge base. Retrieval is keyword matching through `GroundedContext` (`core.py:70-84`). Questions below assume "Guide" means a document-answering agent; it is not one, so most items are NOT_PRESENT by design.

| Requested property | Status | Evidence |
|---|---|---|
| Document provenance | PARTIAL | Receipt carries query hash, source ids, `context_version` (`core.py:170-178`) but sources are code constants; no author/origin/URL/page. `SourceRef.content_hash` exists (`app/context.py:88-97`). |
| Versioning | PARTIAL | `context_version` is a content hash of the set (immutable snapshot id), not a doc version history. APP_DOCS changes with each deploy; no changelog. |
| Access control | IMPLEMENTED | RBAC role -> visible apps (`core.py:40-53`), unknown role -> viewer, derived server-side (`app.py:51-95`). Tests: `test_role_derivation.py` (8), `test_operator.py::test_retrieve_is_rbac_scoped`, `::test_n6_unknown_role_is_least_privilege_not_admin`. |
| Unsupported-claim escalation | NOT_PRESENT | Only `no_match` returns "No matching document found" (`core.py:156-158`; test `test_no_match_answers_honestly_and_cites_nothing`). No check that the LLM narration stays within the cited text; the prompt says "ONLY" but no verification (`core.py:159-163`). No human handoff/ticket. |

Gaps:
- G1 (CONFIRMED, MEDIUM) LLM answer is not validated against cited cards; `cited` is the retrieval set, not what the answer actually used (`core.py:169-171`).
- G2 (CONFIRMED, LOW) Citations link to `https://demo.redevops.io/m/<app>` constant (`core.py:18`) regardless of deployment.
- G3 (CONFIRMED, MEDIUM) Scope has no `tenant_id` (`core.py:97`), see section 6.

---

## 6. Tenant isolation (scenario 8)

Note: there is no "archive lookup" feature in this repo (`grep archive` hits only visualization/capture archive flags). I assessed the equivalents: retrieval (`GroundedContext`), mission traces/events, and receipts.

### 6.1 Retrieval
- Mechanism: `GroundedContext._item_allowed` (`agentic_os/app/context.py:170-187`) fails closed when scope has a tenant: item tenant must equal scope tenant, untagged items are rejected. App isolation needs an explicit cross-app grant. Tests CONFIRMED present: `tests/test_app_context.py::test_tenant_isolation_a_cannot_read_b`, `::test_app_isolation_needs_explicit_cross_app_grant_not_a_shared_tenant`, `::test_permission_scoping_fails_closed`, conformance `tests/conformance/test_conformance_retrieval.py`.
- Weakness T1 (CONFIRMED, HIGH): legacy-open default. If the caller does not pass a tenant, untagged and any-tenant items are admitted (`context.py:171-173` docstring, `:175`). Isolation is opt-in by the caller, not by the store. Fix: require `tenant_id` unless an explicit `single_tenant=True` is configured; refuse otherwise.
- Weakness T2 (CONFIRMED, HIGH): the only production consumer, Guide, builds `RetrievalScope(app_id="guide", ...)` with no tenant (`apps/guide/core.py:97`), and its docs carry no `tenant_id` (`core.py:76-80`). Fine today because the cards are public, but any future tenant-specific doc added to Guide would be cross-tenant readable.
- Weakness T3 (INFERRED, MEDIUM): `_item_allowed` trusts `item["tenant_id"]` from the retriever; a retriever bug or shared index mislabels data. No second check at the store level.

### 6.2 Traces and mission events
- Mission API: 404 for unknown AND other tenant's mission (`mission/api.py:49-52`); events/explain/plans all call `_authorize_mission` (`:92-190`); lists filtered; tenant token forces its own tenant on create (`:68-80`). Tests CONFIRMED: `tests/test_mission_api_default_deny.py::test_cross_tenant_rejected_without_existence_leak_or_effect`, `::test_tenant_cannot_touch_untenanted_admin_mission_and_lists_are_filtered`, `::test_anonymous_cannot_enumerate_tenant_missions`, `::test_gated_reads_are_tenant_scoped`, `::test_tenant_token_forces_tenant_on_create`.
- Weakness T4 (CONFIRMED, MEDIUM): isolation is at the API layer only. `EventStore` is keyed by mission id with no tenant column (`mission/store.py`; grep tenant = 0), and `durable_sink` writes security events under `TELEMETRY_SCOPE="__telemetry__"` when no mission id (`security_monitor.py:116-141`). Anyone with store access (Edge Sentinel poller, compliance projections) sees all tenants. Fix: tenant column + per-tenant partition/RLS; telemetry scoped per tenant.
- Weakness T5 (CONFIRMED, MEDIUM): `GET /capabilities` is `@public` and `/learning` is admin_only (`api.py:~200-215`); capability discovery is global. Low risk but leaks which operators/capabilities exist.
- Edge Sentinel / Privacy / Books / Billing / Support / Compliance apps: single-tenant by construction (`TENANT` constants like `"Meridian Wealth Management"`, `books/core.py:45`). Cross-tenant isolation is by deployment, not code. CONFIRMED.
- The one real per-tenant key scoping: Books idempotency tokens hash `TENANT|_tenant|COMPANY` (`books/core.py:~625`; test `test_keys_are_scoped_to_tenant`).

### 6.3 Receipts
- Retrieval receipt: includes `scope` dict (tenant/app/principal) and a receipt id hashed over tenant and app (`context.py:236-240`), no raw content. Test `test_receipt_is_reproducible_and_carries_no_raw_content`. Good.
- Integration receipts: `IntegrationReceipt` has no tenant field; tenant only on `Resource` (`integration/contracts.py:74`). CONFIRMED (MEDIUM): receipts cannot be filtered by tenant on their own.
- App receipts (billing/books) are action result dicts returned to the caller; there is no receipt store with tenant scope in the OSS layer. The repo's own docs say multi-tenant governance lives in the enterprise overlay (`integration/contracts.py:9`).
- UNTESTED: no test exercises tenant-filtered reads of integration receipts or telemetry events.

---

## 7. Evidence freshness (scenario 9)

| Mechanism | Evidence | Status |
|---|---|---|
| Retrieval staleness filter | `GroundedContext(freshness_seconds=...)` drops stale items (`context.py:144, 214-218`). Test `test_stale_evidence_is_withheld`. | Implemented but OFF by default (`freshness_seconds=None`) and Guide does not set it (`guide/core.py:81`). |
| Items without a timestamp | `as_of or fetched_at` of 0 never counts as stale (`context.py:215-216`) | CONFIRMED HIGH: fail-open; undated evidence is always fresh. Fix: treat missing `as_of` as stale when a freshness bound is set. |
| Stale marking vs dropping | stale items are withheld, counted in `withheld`, not reported to the user as "evidence too old" | MEDIUM: answer silently narrows. Fix: surface `withheld_stale` in the answer and escalate when it drops below a needed source. |
| Bitemporal observations | Billing observe records `known_at`/`valid_at` with quality flags and documented fallbacks (`apps/billing/observe.py:1-25`; test `test_closed_loop.py::test_observe_documents_fallbacks_when_lago_lacks_fields`) | Implemented (billing only). |
| Receivables decay | Dashboard cached 15s (`billing/core.py:79`, `books/core.py:99`); `chase_overdue` forces refresh (`fetch_activity(force=True)`), good. `days_overdue` uses `date.today()` local clock (`billing/core.py:103-114`) | LOW: timezone drift; fine. |
| Threat intel decay | `enrichment.is_stale` + `reconcile_finding` downgrade a finding when its backing intel is stale/revoked (`edge-sentinel/enrichment.py:42-48,136-150`; tests `test_enrichment.py`) | Implemented, good. |
| Compliance scan age | No scan timestamp surfaced as a freshness bound; results file is read as-is (`core.py:104 scap_connected`, size > 0 only) | CONFIRMED MEDIUM: a month-old scan renders as current; "Control pass rate" has no as-of. Fix: show `scanned_at`, mark stale after N days, block "audit-ready" claims when stale. |
| Compliance SME expiries | computed against `date.today()` but data are constants | see 2.6 |
| Verification goes stale | `tests/test_deal_closing_contracts.py::test_verification_goes_stale`, `test_deal_closing_blockers.py::test_stale_becomes_stale_evidence_blocker` | Implemented in deal-closing, a different domain. |

---

## 8. Cross-cutting top risks (this workstream)
1. No agent-assurance wired in Edge Sentinel: detectors are test-only, no runtime subscription, no suspend/revoke path (1.5, 1.6).
2. Approval authority = one shared Basic credential labelled `operator-basic` across apps (2.3).
3. No invoice or tax state machine and no tax capability; finance "AI" is read + stage + a few governed writes (4.7, catalog).
4. Reconciliation is first-exact-amount match and posts nothing; dashboard dunning reports false success (4.3, 4.4).
5. Tenant isolation and freshness are opt-in by the caller; undated evidence is always "fresh" (6.1, 7).
6. Privacy Lago lookup misses subjects beyond 100 customers; erasure note over-claims anonymization (3.2).
