# App boundaries and the business-admin capability catalog (workstream D)

Baseline audit reports, 2026-10-23. Findings and their status: [README.md](README.md).

## Contents

- [App boundaries audit (workstream D) — 2026-10-23](#app-boundaries-audit-workstream-d--2026-10-23) (was `app-boundaries.md`)
- [Business-admin capability catalog audit (workstream D) — 2026-10-23](#business-admin-capability-catalog-audit-workstream-d--2026-10-23) (was `business-admin-catalog.md`)

## App boundaries audit (workstream D) — 2026-10-23

Scope: Edge Sentinel, Compliance, Privacy, Revenue/Finance, Guide, tenant isolation (scenario 8), evidence freshness (scenario 9).
Method: static read of source + test names. No tests were executed, no docker, no source modified.
Labels: CONFIRMED = read in code (file:line). INFERRED = follows from code but needs a live system to prove. UNTESTED = no test found.
Severity: CRITICAL / HIGH / MEDIUM / LOW.

Paths are relative to `/Users/arybach/agentic-os`.

---

### 1. Edge Sentinel (`apps/edge-sentinel`)

#### 1.1 What it is
A CrowdSec wrapper (decisions via LAPI, alerts via `docker exec cscli`) plus a typed evidence/case spine (`evidence.py`, `case_store.py`, `incident.py`), STIX/ATT&CK/CTI, hunt, detection-engineering, DFIR, and governed response. Its own ARCHITECTURE.md is honest that "agent-security monitoring" is NOT yet delivered (`apps/edge-sentinel/ARCHITECTURE.md`, "NOT yet delivered" paragraph). The code agrees with that.

#### 1.2 Telemetry that exists
| Source | Evidence | Real or sample |
|---|---|---|
| CrowdSec decisions (LAPI) and alerts (cscli) | `core.py:86 fetch_decisions`, `core.py:102 fetch_alerts` | Real when CrowdSec reachable |
| NetBird peers/policies | `netbird.py` header | SAMPLE data until `NETBIRD_API_TOKEN` set (labelled) |
| Portabase backup coverage | `portabase.py` header | SAMPLE until token set (labelled) |
| deploy-scan (nmap/nuclei/ZAP) proxy | `deploy_scan.py` header | Real only if `DEPLOY_SCAN_MCP_URL` set |
| Runtime events (agent telemetry) | `ai_security.py:51 observation_from_runtime_event` | Library only — see 1.5 |

#### 1.3 Coverage gaps
- E1 (CONFIRMED, MEDIUM) `fetch_alerts` returns `[]` on any non-zero rc or empty stdout (`core.py:102-111`). "CrowdSec CLI failed / docker socket missing / sudo prompt" is indistinguishable from "no alerts". Fix: return `(alerts, status)` and surface a "telemetry unavailable" state in KPIs, and raise a self-alert after N consecutive empty-with-error polls.
- E2 (CONFIRMED, MEDIUM) Alert transport depends on `sudo docker exec` (`core.py:48-64`). Inside the container it needs the docker socket mounted (root-equivalent on host). Fix: use LAPI machine-login for alerts and drop the socket mount.
- E3 (CONFIRMED, LOW) Only host/network perimeter telemetry. No sensors for apps in this repo (billing/books/etc.), no auth-failure feeds from the Mission API, no gate-notification or approval anomalies.

#### 1.4 Alert routing
CONFIRMED (HIGH): there is no outbound alert routing. `grep -i "notify|webhook|slack|pager|email"` across `apps/edge-sentinel/*.py` (non-test) finds nothing but UI strings. Alerts are a dashboard feed (`app.py` `_alert_feed`) and a case store. The only paging-like mechanism in the repo is the Mission Runtime `GateNotifier` (`agentic_os/mission/gate_notify.py`), which notifies on approval gates, not on security findings. Fix: route SecurityCase creation (severity >= HIGH) through GateNotifier-style outbox with dedupe key = case id.

#### 1.5 Can it pause or revoke runs?
CONFIRMED (HIGH): No. Edge Sentinel's operator exposes exactly three capabilities: `sentinel.triage`, `sentinel.block_ip`, `sentinel.unblock_ip` (`operator.py:26-59`). All side effects are CrowdSec IP bans. It imports only `operator_sdk`, never `MissionRuntime`.
- The runtime does have `MissionRuntime.suspend()` (`agentic_os/mission/runtime.py:1364`) and `cancel_mission` (`runtime.py:371`) and a credential broker with revoke (`agentic_os/secrets/stores.py:6`, `agentic_os/provisioning/identity.py`). Only `agent_gateway/mission_adapter.py:75` calls `suspend`. No HTTP route in `mission/api.py` exposes suspend, and nothing connects a security finding to either.
- `ai_security.py` docstring says it "cannot (and does not) re-drive the denied action"; it is an observer by design (`ai_security.py:12-16`).
Fix: add a governed `sentinel.suspend_mission` / `sentinel.revoke_grant` capability bound to `MissionRuntime.suspend` and the secret store `revoke`, approval-gated, with verification by read-back of mission state.

#### 1.6 Agent-assurance sensors (the requested list)
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

#### 1.7 Evidence quality in the governed response path (positive)
- CONFIRMED: `propose_response` refuses side-effecting capabilities that are not approval_required (`response.py:61-73`); `execute_response` refuses without an approved decision (`response.py:76-85`); receipt and verification are separate and verification does parsed-IP equality (`response.py:111-137`). Tests: `test_response.py`, `test_block_guard_and_approval_state.py`.
- Residual (INFERRED, MEDIUM): approval authority is a single shared Basic credential per deploy (see 2.3), so "SecurityDecision.actor" is not a person.

#### 1.8 Risk handoff to Compliance
CONFIRMED (MEDIUM): `risk_bridge.py:9-12` states there is no Compliance receiver. `RiskHandoff` is produced and goes nowhere. Compliance app (`apps/compliance`) has no endpoint accepting it.

---

### 2. Compliance (`apps/compliance`, `agentic_os/compliance`)

#### 2.1 What it is
CONFIRMED: an OpenSCAP/CIS-Ubuntu scan viewer plus a hash-chained consent ledger, plus 4 hard-coded "SME items" (licenses/insurance). README markets "Vanta/Drata style" and "audit-ready evidence" (README line ~14; Guide card `apps/guide/core.py:28`). Product capability is far narrower than that wording.

#### 2.2 Policy versions
NOT_PRESENT (CONFIRMED, HIGH). There is no policy/control document model, no version, no effective-date, no mapping of controls to frameworks other than the single scanned profile (`core.py:90 FRAMEWORK`). Only SSG release/hash of the scanned datastream is recorded (`core.py:77-82 _ssg_verification`; test `test_scan_records_which_ssg_hash_was_verified`). Mission-level policy changes are logged by the runtime (`PolicyChanged` events consumed by `agentic_os/compliance/evidence_producers.py:_GOVERNANCE_TYPES`), but the app does not surface them.
Fix: add `PolicyVersion{id, version, effective_from, owner, content_hash, supersedes}` to the ledger and bind each scan/approval to the version in force.

#### 2.3 Approval authority
CONFIRMED (HIGH): approval is "request carries valid operator HTTP Basic credentials" (`gate.py:19-47`). `OPERATOR_MARKER = {"approved": True, "by": "operator-basic"}` (`gate.py:19`) — every approval is attributed to the same literal actor. No per-person identity, no role, no separation of duties, no delegation, no expiry. Same pattern in Edge Sentinel/Privacy/Billing/Books/Support.
Mission API does better: `api.py:124-139` rejects approval from a requester credential when `AGENTIC_OS_APPROVER_TOKENS` is set (separation of duty), records `actor=role:tenant`; test `tests/test_mission_api_default_deny.py::test_approver_tokens_block_requester_and_allow_approver`. But when approver tokens are not configured the requester can approve (`::test_requester_can_approve_when_no_approver_tokens_configured`) — CONFIRMED MEDIUM; and the app-level routes bypass that path entirely.
Forgery resistance (positive): `test_agent_run_forgery.py::test_agent_run_remediate_stays_staged`, `test_route_gating.py::test_forged_approval_in_body_is_still_staged`.
Fix: make apps accept the signed approval token from `docs/APPROVAL_TOKENS.md` with `sub` = person; fail closed when only a shared Basic credential is configured for CONSEQUENTIAL actions.

#### 2.4 Evidence retention
NOT_PRESENT (CONFIRMED, MEDIUM). The consent ledger is append-only JSONL with no retention, rotation, archival or legal-hold concept (`core.py:50-52, 500-512`). `results/scan-results.xml` is overwritten per scan (single file, `core.py` SCAP_RESULTS), so scan history is lost. Positive: tamper-evidence works — `core.py:515 verify_chain`; tests `test_ledger_chain_verifies_and_detects_tampering`, `test_ledger_concurrent_appends_keep_chain_valid`.
Gap: chain verification only checks internal consistency; the head hash is not anchored anywhere external, so deleting the whole tail (truncate) is undetectable (INFERRED, MEDIUM; `verify_chain` has no expected-head parameter).
Fix: periodically publish head hash to an external anchor (e.g. event store + off-box), keep dated scan archives.

#### 2.5 Audit export
NOT_PRESENT in the app (CONFIRMED, MEDIUM): routes are only `/health`, `/api/agent`, `/api/activity`, `/`, `/agent/run` (`app.py:469-497`) plus `/report` HTML. No export of ledger, scan history or approvals. A library projection exists — `agentic_os/compliance/evidence_producers.py` turns runtime events into `ControlEvidence` with ENFORCED/EVIDENCED discipline (tests `tests/test_compliance_evidence_producers.py::test_no_events_produces_nothing_not_a_false_pass`, `::test_hitl_approval_evidenced_only_when_a_human_acted`) — but nothing in `apps/compliance` calls it (grep: only `agentic_os/compliance/__init__.py`).

#### 2.6 Hard-coded demo data presented as live
CONFIRMED (HIGH): `SME_ITEMS` constants (`core.py:95-100`) feed the "expiring" queue. The E&O policy expiry is `2026-09-08`, already past on 2026-10-23, yet `_sme_rows` only computes `days <= 90` -> label EXPIRING (`core.py:193-196`); a negative `days` is never labelled EXPIRED/LAPSED. Fix: add EXPIRED state, move items to a real store/connector.

---

### 3. Privacy / DSAR (`apps/agentic-privacy`)

Strongest app in this scope. 33+ named tests in `test_dsar_hardening.py`.

#### 3.1 What works (CONFIRMED)
- Intake opens a PENDING DSAR, unique id with random suffix (`core.py:137-141`), `due_at = now + SLA_DAYS (30)` (`core.py:59, 748`).
- Single-use verification token, expiry, POST-consume vs GET-no-consume: tests `test_verify_wrong_token_replay_and_expiry`, `test_get_verify_does_not_consume_token_post_does`.
- Access is emailed, not returned: `test_access_after_verify_is_emailed_not_returned`, `test_access_export_token_is_single_use_and_expires`.
- Erasure stages unless verified+matching+approved: `test_delete_stages_unless_verified_matching_and_approved`; audit failure blocks erasure: `test_audit_failure_blocks_erasure`; partial erasure is reported as partial: `test_residual_yields_partial_erasure_and_honest_outputs`.
- Hash-chained audit log: `core.py:203 verify_audit_chain`.
- Rate limits and mail-bomb cap: `test_ip_token_bucket_429_with_retry_after`, `test_per_email_intake_cap_stops_mail_bombing`.
- LLM sees counts only: `test_llm_receives_counts_and_categories_only`.

#### 3.2 Gaps
- P1 (CONFIRMED, HIGH) Lago subject lookup reads only the first 100 customers: `lago_find` uses `?per_page=100` with no pagination (`core.py:433-447`). A subject beyond customer #100 is silently "not found", so access/erasure reports can be wrong while looking clean. Fix: paginate (`_get_all` pattern in `apps/billing/core.py:57`) and filter server-side by `external_id`/email.
- P2 (CONFIRMED, MEDIUM) Over-claim in billing erasure: the note says "PII anonymized on request" (`core.py:461`) but `lago_delete` performs no anonymization call; it returns `deleted: 0, retained: N`. Fix: either call Lago customer update to redact PII fields, or change the note to "retained, not anonymized".
- P3 (CONFIRMED, MEDIUM) SLA is displayed, never enforced: `overdue` is a count in KPIs (`core.py:1183-1198`); no escalation, reminder or breach event. Fix: emit an overdue event/obligation at `due_at - 5d` and `due_at`.
- P4 (CONFIRMED, MEDIUM) Retention scan covers one system and one object type: ERPNext Leads not Converted (`core.py:1105-1120`). Lago/Chatwoot/Listmonk/S3 are not scanned. Purge swallows per-record delete errors (`except Exception: pass`, `core.py:1117`) and logs `purged_total` only, so failures are not itemized. Hard delete of "uncontacted leads" has no legal-hold check.
- P5 (INFERRED, MEDIUM) No tenant concept in the app (grep `tenant` finds only display strings); one DSAR store per deployment, so multi-tenant use needs a deployment per tenant.
- P6 (CONFIRMED, MEDIUM) Approval authority same shared Basic credential as 2.3.
- P7 (CONFIRMED, LOW) No DSAR extension/exemption states (GDPR Art. 12(3) extension, manifestly unfounded) — states are PENDING/verified/fulfilled only.

---

### 4. Revenue / Finance integrations

Detailed per-capability evidence is in `audit/business-admin-catalog.md`. This section is the boundary and semantics view.

#### 4.1 Which system is the source of truth
| Object | Where it lives | Rule in code | Gap |
|---|---|---|---|
| Customers/subscriptions/invoices (SaaS) | Lago | Billing reads invoices and `payment_status` from Lago (`apps/billing/core.py:86-100`) | Display state collapses status/payment/overdue into one label; no explicit state machine |
| GL, AR/AP, bank txns | ERPNext | Books reads GL, Sales/Purchase Invoice, Bank Transaction (`apps/books/core.py:205-250`) | Books is single-company via env `COMPANY` (`core.py:43`) |
| Pipeline/contacts | Twenty / ERPNext CRM | `agentic_os/world/adapters.py:159-224` | Upsert by title only |
| Conversations | Chatwoot | `apps/support/core.py`, `agentic_os/integrations/chatwoot.py:83` | |
| Metric definitions | `SemanticRegistry` | five revenue metrics deliberately distinct, no single "truth" (`agentic_os/integration/semantics.py:100-121`) | Source priorities name stripe/quickbooks/salesforce which have NO adapters here (`connector_capabilities.yaml:116-128` lists quickbooks/xero/salesforce as UNSUPPORTED) |

No document anywhere declares a cross-system source-of-truth rule for the same object (e.g. invoice in Lago vs Sales Invoice in ERPNext). The two systems are never joined: no code links a Lago invoice to an ERPNext Sales Invoice or Payment Entry. CONFIRMED (HIGH).

#### 4.2 Integration semantics (positive core)
- Obligation engine: write, then independently read back, classify satisfied / unsatisfied / conflicting / overdue (`agentic_os/integration/obligations.py:68-129`; tests `tests/test_integration_obligation.py`). Receipt is only issued on observed state.
- Connector capability registry fails closed: only VERIFIED is `usable` (`business/capabilities.py:21-35`). Statement "verification: contract" means fixtures, not live accounts (`connector_capabilities.yaml:14-17`).
- Gap (CONFIRMED, MEDIUM): `obligations.py:97` compares deadline and now as strings (`now > obligation.deadline`); correct only if both are same-format ISO UTC; also the overdue check happens only after retries are exhausted, so an obligation can be reported UNSATISFIED rather than OVERDUE depending on order. Fix: parse to datetimes and test deadline first.

#### 4.3 Invoice/payment matching
- `books.reconcile` matches the FIRST open A/R invoice to the FIRST deposit with identical cents (`apps/books/core.py:379-400`). No customer, reference, currency, date, or one-to-many/partial-payment logic. Result string says "staged for posting" but there is no posting capability, so nothing is written. CONFIRMED HIGH. Test: `apps/books/test_contracts.py::test_reconcile_compares_to_the_cent`, `apps/books/test_operator.py::test_reconcile_matches_deposit_to_invoice` (assert the proposal only).
- Payout reconciliation (`agentic_os/integration/reconciliation.py:47`) enforces `charges - refunds - fees - disputes + adjustments == payout == deposit` with explicit tolerance and unknown-type surfacing. It is Stripe-balance-transaction shaped and lives under `integration/experiments/payout_reconciliation.py`; not wired to any app. Tests `tests/test_experiment_payout_reconciliation.py` (7 tests).
- Categorize is a tag, not accounting: sets `reference_number` and hard-codes category by direction ("Revenue · Sales" for deposits, "Materials · COGS" for withdrawals) (`apps/books/core.py:325`). Result is honestly labelled `mode: tag_only`. CONFIRMED MEDIUM (name overstates).
- Close gate vs agent capability mismatch (INFERRED, MEDIUM): the close checklist requires "Bank transactions reconciled" (`core.py:253-265`), but no agent action reconciles; so an agent-only loop can never reach a ready close. Fix: add a governed Payment Entry / Bank Transaction reconcile capability, or drop it from agent-owned blockers.

#### 4.4 Approval gates and receipts
- Money-moving actions are gated in core, not in the route: `chase_invoices` (`billing/core.py:289`), `stage_refund` (`:486`), `create_subscription` (`:543`), `books.close`, `record_revenue`. Tests: `apps/billing/test_refund_and_forgery.py`, `test_route_gating.py`, `apps/books/test_route_gating.py`.
- Verified outcomes (positive): dunning reads each invoice back (`billing/core.py:340-356`), `partial` is never `done` (tests `test_closed_loop.py::test_partial_settlement_fails_the_node_and_is_reported_partial`); close reads back the PCV and GL (`books/core.py:484-505`; test `test_close_executes_verifies_idempotent_and_rejects_inconsistent`).
- NOT verified (CONFIRMED): refund reports success from the POST response, no read-back of the credit note (`billing/core.py:486-527`); `create_subscription` same (`:543-606`).
- Legacy dunning path always returns `status: "done"` regardless of Lago HTTP result and its summary says "dunning reminder sent" (`billing/core.py:201-258`, esp. 248-252). Dashboard button `_chase_overdue` uses this path (`billing/app.py:344-346`). HIGH: false-success on the human-facing surface. Fix: route the dashboard to `chase_invoices` or apply the same read-back and status logic.
- Refund double-spend guard (INFERRED, MEDIUM, UNTESTED): refundable = sum of positive fee `amount_cents` (`billing/core.py:461`), without subtracting existing credit notes; relies on Lago to reject over-credit. Fix: read existing credit notes and cap.

#### 4.5 CRM / Chatwoot / Lago read paths
- ERPNext open receivables: hard `limit=200` with no order (`integrations/erpnext.py:109`), so AR above 200 invoices is silently truncated; currency not carried (rows only hold cents) (`:127-133`). CONFIRMED MEDIUM. Mixed-currency materiality sums amount x days across currencies (`business/missions.py:124`) CONFIRMED MEDIUM.
- Receivable cases hard-code `delivery_state=DELIVERED` and omit dispute/support state (`commercial/receivables_sources.py:65`), so from live ERPNext every overdue invoice resolves to COLLECT (test `test_erpnext_receivables_source.py::test_genuinely_overdue_case_resolves_to_collect`). The "reason across delivery/dispute/support" claim in `receivables.py` docstring is true only for the pure function, not for the live feed. CONFIRMED HIGH.
- Lago expansion scan: `usage_ratio` = current usage / plan recurring amount; healthy-account gate defaults to True (`integrations/lago.py:70-100`). Tests `tests/test_lago_expansion_source.py`.

#### 4.6 LLM and money
CONFIRMED: no code path lets an LLM compute tax, totals, refunds or amounts. LLM calls in billing/books/support are narration only (`billing/core.py:226 blurb`, `books/core.py:353`), routed through `governed_text`. The model planner emits outcomes and needs only (`mission/planner.py:107-139`), no numeric inputs. Quote totals are deterministic integer math (`revenue/quote.py:85-153`). Residual (INFERRED, LOW): LLM narration can quote wrong figures to an approver; the figures are not machine-checked against the staged proposal. Fix: render the approver summary from structured fields, show LLM text as clearly secondary.

#### 4.7 State machines (see catalog for detail)
Neither state machine exists. CONFIRMED (HIGH): no invoice lifecycle DRAFT->VALIDATED->APPROVED->ISSUED->DELIVERED->PAID, no tax-obligation lifecycle OBSERVED->EVIDENCE_REQUIRED->REVIEW_REQUIRED->SUBMITTED->ACCEPTED. `grep` for TRANSITIONS/StateMachine/can_transition in `agentic_os` and `apps` returns nothing. What exists: a free-text `Invoice.status` comment vocabulary (`business/contracts.py:149`), a display collapse (`billing/core.py:86`), order stage chain monitored for missed transitions (`commercial/order.py:17,42-61`), collection stages (`commercial/receivables.py:90-100`). These monitor, they do not enforce.

---

### 5. Guide (`apps/guide`)

#### 5.1 What it is
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

### 6. Tenant isolation (scenario 8)

Note: there is no "archive lookup" feature in this repo (`grep archive` hits only visualization/capture archive flags). I assessed the equivalents: retrieval (`GroundedContext`), mission traces/events, and receipts.

#### 6.1 Retrieval
- Mechanism: `GroundedContext._item_allowed` (`agentic_os/app/context.py:170-187`) fails closed when scope has a tenant: item tenant must equal scope tenant, untagged items are rejected. App isolation needs an explicit cross-app grant. Tests CONFIRMED present: `tests/test_app_context.py::test_tenant_isolation_a_cannot_read_b`, `::test_app_isolation_needs_explicit_cross_app_grant_not_a_shared_tenant`, `::test_permission_scoping_fails_closed`, conformance `tests/conformance/test_conformance_retrieval.py`.
- Weakness T1 (CONFIRMED, HIGH): legacy-open default. If the caller does not pass a tenant, untagged and any-tenant items are admitted (`context.py:171-173` docstring, `:175`). Isolation is opt-in by the caller, not by the store. Fix: require `tenant_id` unless an explicit `single_tenant=True` is configured; refuse otherwise.
- Weakness T2 (CONFIRMED, HIGH): the only production consumer, Guide, builds `RetrievalScope(app_id="guide", ...)` with no tenant (`apps/guide/core.py:97`), and its docs carry no `tenant_id` (`core.py:76-80`). Fine today because the cards are public, but any future tenant-specific doc added to Guide would be cross-tenant readable.
- Weakness T3 (INFERRED, MEDIUM): `_item_allowed` trusts `item["tenant_id"]` from the retriever; a retriever bug or shared index mislabels data. No second check at the store level.

#### 6.2 Traces and mission events
- Mission API: 404 for unknown AND other tenant's mission (`mission/api.py:49-52`); events/explain/plans all call `_authorize_mission` (`:92-190`); lists filtered; tenant token forces its own tenant on create (`:68-80`). Tests CONFIRMED: `tests/test_mission_api_default_deny.py::test_cross_tenant_rejected_without_existence_leak_or_effect`, `::test_tenant_cannot_touch_untenanted_admin_mission_and_lists_are_filtered`, `::test_anonymous_cannot_enumerate_tenant_missions`, `::test_gated_reads_are_tenant_scoped`, `::test_tenant_token_forces_tenant_on_create`.
- Weakness T4 (CONFIRMED, MEDIUM): isolation is at the API layer only. `EventStore` is keyed by mission id with no tenant column (`mission/store.py`; grep tenant = 0), and `durable_sink` writes security events under `TELEMETRY_SCOPE="__telemetry__"` when no mission id (`security_monitor.py:116-141`). Anyone with store access (Edge Sentinel poller, compliance projections) sees all tenants. Fix: tenant column + per-tenant partition/RLS; telemetry scoped per tenant.
- Weakness T5 (CONFIRMED, MEDIUM): `GET /capabilities` is `@public` and `/learning` is admin_only (`api.py:~200-215`); capability discovery is global. Low risk but leaks which operators/capabilities exist.
- Edge Sentinel / Privacy / Books / Billing / Support / Compliance apps: single-tenant by construction (`TENANT` constants like `"Meridian Wealth Management"`, `books/core.py:45`). Cross-tenant isolation is by deployment, not code. CONFIRMED.
- The one real per-tenant key scoping: Books idempotency tokens hash `TENANT|_tenant|COMPANY` (`books/core.py:~625`; test `test_keys_are_scoped_to_tenant`).

#### 6.3 Receipts
- Retrieval receipt: includes `scope` dict (tenant/app/principal) and a receipt id hashed over tenant and app (`context.py:236-240`), no raw content. Test `test_receipt_is_reproducible_and_carries_no_raw_content`. Good.
- Integration receipts: `IntegrationReceipt` has no tenant field; tenant only on `Resource` (`integration/contracts.py:74`). CONFIRMED (MEDIUM): receipts cannot be filtered by tenant on their own.
- App receipts (billing/books) are action result dicts returned to the caller; there is no receipt store with tenant scope in the OSS layer. The repo's own docs say multi-tenant governance lives in the enterprise overlay (`integration/contracts.py:9`).
- UNTESTED: no test exercises tenant-filtered reads of integration receipts or telemetry events.

---

### 7. Evidence freshness (scenario 9)

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

### 8. Cross-cutting top risks (this workstream)
1. No agent-assurance wired in Edge Sentinel: detectors are test-only, no runtime subscription, no suspend/revoke path (1.5, 1.6).
2. Approval authority = one shared Basic credential labelled `operator-basic` across apps (2.3).
3. No invoice or tax state machine and no tax capability; finance "AI" is read + stage + a few governed writes (4.7, catalog).
4. Reconciliation is first-exact-amount match and posts nothing; dashboard dunning reports false success (4.3, 4.4).
5. Tenant isolation and freshness are opt-in by the caller; undated evidence is always "fresh" (6.1, 7).
6. Privacy Lago lookup misses subjects beyond 100 customers; erasure note over-claims anonymization (3.2).

## Business-admin capability catalog audit (workstream D) — 2026-10-23

Status vocabulary: IMPLEMENTED (product code does it, with tests) / PARTIAL (some of the row, gaps named) / CONNECTOR_ONLY (the underlying OSS core does it; AOS adds no agent capability) / NOT_PRESENT / UNKNOWN.
Finding labels: CONFIRMED (read in code) / INFERRED (follows from code, needs live proof) / UNTESTED. Severity: CRITICAL/HIGH/MEDIUM/LOW.
Method: static read; tests were NOT executed. "Test" = test exists by that name; I did not run it.
Paths relative to `/Users/arybach/agentic-os`. Product capability and README/marketing claim are shown separately.

Key marketing sources: Guide card text `apps/guide/core.py:22-37` (what users are told), app READMEs (`apps/billing/README.md`, `apps/books/README.md`, `apps/compliance/README.md`), and canned demo `deploy/module_service.py` (explicitly "mock data", `deploy/module_service.py:1-12`).

### Summary

| # | Row | Status |
|---|---|---|
| 1 | Invoice creation + dispatch | CONNECTOR_ONLY |
| 2 | Overdue receivables / collections | PARTIAL |
| 3 | Payment matching + reconciliation | PARTIAL |
| 4 | Quote -> order -> delivery -> invoice | PARTIAL |
| 5 | VAT/GST/sales-tax registration + thresholds | NOT_PRESENT |
| 6 | Tax calculation + exemptions | NOT_PRESENT |
| 7 | Periodic tax return prep / filing status | NOT_PRESENT |
| 8 | E-invoicing / credit notes | PARTIAL (credit note via refund only; e-invoicing absent) |
| 9 | Accounts payable / vendor bills | PARTIAL (read-only) |
| 10 | Expense claims / month-end close | PARTIAL (annual PCV close implemented; expenses absent) |
| 11 | Subscription lifecycle + billing failures (Lago) | PARTIAL |
| 12 | Contracts / renewals / certificates | PARTIAL (NDA drafting only) |
| 13 | Customer onboarding / KYC | PARTIAL (onboarding saga yes; KYC simulated) |
| 14a | Privacy requests (DSAR) | IMPLEMENTED |
| 14b | Retention | PARTIAL |
| 15 | Payroll | NOT_PRESENT |
| 16 | Licensing / permits | NOT_PRESENT (demo constants only) |
| 17 | Support SLA / customer communication | PARTIAL |
| 18 | Supplier / procurement follow-up | PARTIAL (analytics only) |

Counts (19 entries, row 14 split): IMPLEMENTED 1, PARTIAL 12, CONNECTOR_ONLY 1, NOT_PRESENT 5, UNKNOWN 0.

---

### State machines and LLM arithmetic (asked separately)

#### Invoice state machine DRAFT->VALIDATED->APPROVED->ISSUED->DELIVERED->PAID
NOT_PRESENT (CONFIRMED, HIGH). Searched `TRANSITIONS`, `ALLOWED_TRANSITION`, `can_transition`, `StateMachine`, `VALIDATED`, `ISSUED` across `agentic_os/` and `apps/`: no hits. What exists instead:
- `Invoice.status` is a free-text field with a comment "draft | open | paid | void | uncollectible" (`agentic_os/integrations/business/contracts.py:149`). No guard, no transition table, no frozen-history check (objects are frozen dataclasses, so a new observation is a new object, not a validated transition).
- Billing display collapse: Lago status/payment_status/overdue -> DRAFT/SENT/PAID/OVERDUE/FAILED label (`apps/billing/core.py:86-100`). Read-only presentation; VALIDATED, APPROVED, DELIVERED do not exist. Lago itself owns draft->finalized.
- Collection lifecycle `identified -> actioned -> promised -> paid -> verified` (`agentic_os/commercial/receivables.py:90-100`) and order chain `received -> sourced -> reserved -> fulfilled -> invoiced -> verified` (`agentic_os/commercial/order.py:17,42-53`) are `ExpectedTransition` monitors (missed/overdue/diverged detection, `agentic_os/reliability.py:59-90`). They detect lateness; they do not enforce legal moves. Tests: `tests/test_reliability_order.py` (5), `tests/test_commercial_receivables.py::test_collection_lifecycle_transitions`.
Proposed fix: add a pure `InvoiceLifecycle` transition table (DRAFT->VALIDATED requires totals recompute + customer tax id present; VALIDATED->APPROVED requires a human approval token bound to the content digest; APPROVED->ISSUED requires connector write + read-back; ISSUED->DELIVERED requires dispatch receipt; ->PAID requires payment match), reuse `ObligationEngine` for the ISSUED/DELIVERED proofs, and make illegal transitions raise.

#### Tax-obligation state machine OBSERVED->EVIDENCE_REQUIRED->REVIEW_REQUIRED->SUBMITTED->ACCEPTED
NOT_PRESENT (CONFIRMED, HIGH). No tax obligation object exists. Grep for `vat|gst|sales tax|tax return|filing|e-invoic` returns only: SEC EDGAR filing presence for counterparties (`agentic_os/intelligence/adapters/sec_edgar.py`), `tax_id` as an entity-resolution key (`agentic_os/intelligence/families/supplier_resolution.py:23`), the legal-governance label "L4 filing/representation -> professional authorization" (`agentic_os/legal/governance.py:29`), and the consent-ledger "filing" of a GDPR consent record (`apps/compliance/core.py:538`). The nearest reusable machinery: `legal/governance.py` level gate and `legal/review.py` professional-review handoff (`ProfessionalReviewRequest`, `review.py:15`) that returns governed evidence.
Fix: model `TaxObligation{jurisdiction, period, kind, due, state, evidence_refs}` over `ExpectedTransition`, with SUBMITTED gated at the L4 professional-authorization level that already exists.

#### Can an LLM compute tax or totals?
No path found. CONFIRMED (grep of every `governed_text`/`.complete(` call site in `agentic_os/` and `apps/`).
- Billing/Books/Edge Sentinel/Growth/Market: LLM output is a one-sentence narration passed back as `reasoning` (`apps/billing/core.py:226-232`, `apps/books/core.py:353-358`). Amounts in the prompts come from deterministic fields.
- Planner: outcomes/needs/dependencies only, no numeric inputs (`agentic_os/mission/planner.py:107-139`).
- Assistants: tools are keyword-routed; fallback LLM answers from a static primer only (`apps/billing/assistant.py:66-84`).
- Quote totals: integer math from catalog price (`agentic_os/revenue/quote.py:117-120`).
- Residual (INFERRED, LOW): outreach/lifecycle LLM drafts (`apps/outreach-engine/app.py:162,404`, `apps/lifecycle/core.py:134,201,227`) may state prices or dates in free text; no check that figures in drafts match source data. Fix: template numeric fields, validate drafts against a fact sheet before approval.
- Residual (INFERRED, LOW): approver sees LLM prose next to the staged action; the structured fields are the authoritative ones. Show structured diff first.

---

### Row details

#### 1. Invoice creation + dispatch — CONNECTOR_ONLY
- Product: agentic-billing wraps Lago. Invoices are produced by Lago as a side effect of subscriptions: `create_subscription` (`apps/billing/core.py:543-606`, gated at `:561`, idempotent by `external_id`, saga undo `cancel_subscription` `:607`). There is no capability to create, edit, validate, finalize, email or download an invoice. Billing operator capabilities are exactly `billing.summary/dunning/refund/create_subscription/cancel_subscription` (`apps/billing/operator.py:71-114`).
- ERPNext side: Books writes only Journal Entry (`books/core.py:595 record_revenue`) and Period Closing Voucher (`:452`). The world adapter would upsert a `Sales Invoice` with payload `{"title": label}` only (`agentic_os/world/adapters.py:224-232,263`); ERPNext requires customer/items/dates, so INFERRED (MEDIUM, UNTESTED against live) it fails and surfaces as `CoreUnavailable`.
- Dispatch: only through the generic gmail/slack send capabilities (VERIFIED by contract, `connector_capabilities.yaml:30-34`) used for follow-ups (`business/missions.py:139`), not for invoice delivery.
- README claim: Guide card says "Checkout -> invoicing -> reconciliation -> dunning" with actions "checkout", "reconciliation" (`apps/guide/core.py:22`). CONFIRMED over-claim (HIGH): `grep checkout apps/billing` finds nothing; billing has no reconciliation capability.
- Tests (adjacent): `apps/billing/test_contracts.py::test_create_subscription_result_passes_verifier`, `::test_forward_verify_undo_round_trip_targets_the_created_subscription`, `::test_undo_never_cancels_a_subscription_that_already_existed`.
- Fix: either remove "checkout"/"reconciliation"/"invoicing" from marketing text, or add `billing.invoice.finalize` / `invoice.send` with read-back; add Lago invoice dispatch proof via ObligationEngine.

#### 2. Overdue receivables / collections — PARTIAL
- Implemented: scoped dunning = Lago `retry_payment` then per-invoice read-back, `dunning_verified` only if all settle, partial never reported done (`apps/billing/core.py:289-358`; tests `test_closed_loop.py::test_full_chain_happy_path`, `::test_partial_settlement_fails_the_node_and_is_reported_partial`, `::test_rejected_retry_or_nothing_settled_fails_the_node`, `::test_operator_scoping_only_chases_the_requested_invoices`, `::test_no_approval_executes_nothing`). Decision cycle with abstain and outcome attribution (`apps/billing/decision.py`, `test_loop_durability.py`).
- Cross-system assessment (dispute/delivery/support/promise priority) exists as a pure function (`agentic_os/commercial/receivables.py:63-90`, tests `test_commercial_receivables.py` x6) and an exact-content-bound email follow-up draft/send/verify mission (`agentic_os/integrations/business/missions.py:95-158`, tests `test_integration_business_receivables.py` x6).
- Gaps:
  - CONFIRMED HIGH: "dunning" = payment retry on the card/payment provider, not a collections communication sequence. No reminder cadence, dunning levels, late fees, payment plans, write-off, or promise-to-pay tracking.
  - CONFIRMED HIGH: legacy `chase_overdue` always returns `status: "done"` and "dunning reminder sent" regardless of the HTTP result, no read-back (`billing/core.py:201-258`); the dashboard action uses it (`billing/app.py:344-346`). False success on the user-facing path. Fix: delegate to `chase_invoices`.
  - CONFIRMED HIGH: live ERPNext feed forces `delivery_state=DELIVERED` and no dispute/support state (`commercial/receivables_sources.py:65`), so every overdue invoice becomes COLLECT; the README "reason across delivery/dispute/support" claim holds only for the pure function.
  - CONFIRMED MEDIUM: ERPNext AR read capped at 200 rows, unordered (`integrations/erpnext.py:109`); mixed-currency materiality (`business/missions.py:124`).
  - CONFIRMED MEDIUM: two disjoint collection paths (Lago retry vs email follow-up) never meet; no shared receivable case record.

#### 3. Payment matching + reconciliation — PARTIAL
- Implemented: payout invariant engine with tolerance and unknown-type surfacing (`agentic_os/integration/reconciliation.py:47-95`; tests `tests/test_experiment_payout_reconciliation.py` x7); ERPNext bank-transaction vs A/R exact-cents proposal (`apps/books/core.py:379-400`; tests `books/test_contracts.py::test_reconcile_compares_to_the_cent`, `books/test_operator.py::test_reconcile_matches_deposit_to_invoice`).
- Gaps:
  - CONFIRMED HIGH: Books reconcile is first-invoice x first-deposit with equal cents; no party/reference/currency/date, no partial, split or multi-invoice matching, no confidence, no ambiguity handling (two equal-amount invoices -> arbitrary pick). Stages a proposal only; there is no posting capability (no Payment Entry write anywhere; `_DOCTYPE` maps "payment" -> Payment Entry only in the world adapter, with title-only payload, `world/adapters.py:228`).
  - CONFIRMED MEDIUM: summary text says "staged for posting" (`books/core.py:~408`) but nothing stages or posts.
  - CONFIRMED MEDIUM: payout reconciliation is under `integration/experiments/` and not wired into any app (grep for imports: only experiments/testkit).
  - CONFIRMED MEDIUM: Lago payments are never matched to ERPNext receipts; two ledgers remain unlinked.
  - CONFIRMED LOW: `categorize` is a `reference_number` tag with category chosen by direction only (`books/core.py:325`); labelled `mode: tag_only`.
- Fix: score candidates on (party, reference, amount, currency, date window), return top-N with ambiguity flag, add a governed `books.post_payment_entry` with GL read-back, wire the payout invariant to Lago/Stripe settlement data.

#### 4. Quote -> order -> delivery -> invoice — PARTIAL
- Implemented: deterministic quote feasibility with availability, promised date, margin floor and approval thresholds (`agentic_os/revenue/quote.py:85-153`; tests `tests/test_revenue_quote.py` x6, `tests/test_erpnext_quote_source.py` x6 incl. live-optional); canonical `SalesOrder`, `SalesOrderLine`, `Shipment`, `PurchaseOrder`, `GoodsReceipt` objects (`business/order.py`, `business/supply.py`); order-stage monitor received..verified (`commercial/order.py`, tests `test_reliability_order.py`); unanswered-quote leakage from Chatwoot (`integrations/chatwoot.py:83`).
- Gaps: CONFIRMED HIGH no quote creation in ERPNext, no quote->sales order conversion, no delivery note, no sales-invoice-from-order; the chain is monitored not driven. `evaluate_order` is read-only intelligence (`commercial/bindings.py:40-45`). CONFIRMED MEDIUM quote total is tax-exclusive and currency is a caller-supplied default `"USD"` not checked against catalog (`quote.py:85-89`); unit price `round()` is banker's rounding and line total truncates `int(unit_price * qty)` with float qty (`quote.py:116-117`), INFERRED LOW rounding drift on fractional quantities.
- Fix: governed `erp.quote.create` / `order.create` / `invoice.create_from_order` with read-back, Decimal arithmetic, currency and tax in the quote contract.

#### 5. VAT/GST/sales-tax registration + thresholds — NOT_PRESENT
- CONFIRMED (HIGH). No registration, nexus, or threshold model anywhere (grep above). Only demo fixture: static row "Sales tax accrual / Q2 estimate / on track" in the canned mock dashboard (`deploy/module_service.py:179`), which is explicitly mock data (`:1-12`) — presents a tax status that no code computes.
- Fix: do not ship the fixture row in any tenant-visible build; add a jurisdiction registry only when a tax engine/connector exists.

#### 6. Tax calculation + exemptions — NOT_PRESENT
- CONFIRMED (HIGH). No tax rate, tax id validation, exemption certificate, or tax line anywhere. `InvoiceLine`/`Invoice` have no tax fields (`business/contracts.py:132-153`). Quote totals are pre-tax (`revenue/quote.py:117-136`). Lago and ERPNext both compute tax natively; AOS neither reads nor validates it. UNKNOWN whether the tenant's Lago/ERPNext tax config is correct — nothing inspects it.
- Fix: read-only tax-line extraction + check that invoice total == sum(lines)+tax from the core; never compute tax in the agent; use a certified engine/connector.

#### 7. Periodic tax return prep / filing status — NOT_PRESENT
- CONFIRMED (HIGH). No return, period, or filing object. See "tax-obligation state machine" above. Closest: PCV period close for ERPNext fiscal year (row 10) and the professional-authorization L4 gate in legal governance (`legal/governance.py:29`).

#### 8. E-invoicing / credit notes — PARTIAL
- Credit notes: IMPLEMENTED for refunds — creates a Lago credit note, refund amount capped at refundable fee total, currency must match, exact cents, approval-gated (`apps/billing/core.py:428-483`, `:486-527`; tests `test_refund_and_forgery.py::test_credit_note_equals_requested_amount_or_cap`, `::test_over_request_is_rejected_without_a_credit_note`, `test_contracts.py::test_refund_currency_must_match_the_invoice`, `::test_refund_is_refused_when_the_invoice_currency_is_unknown`, `::test_absurd_refund_amounts_are_rejected_not_raised`). Stripe/Polar refund executors are contract-verified connectors (`connector_capabilities.yaml:46-56`).
- E-invoicing (Peppol/ZATCA/Facturae/clearance): NOT_PRESENT (CONFIRMED HIGH, no code).
- Gaps: CONFIRMED MEDIUM refund result taken from the POST response, no read-back of the credit note (`billing/core.py:520-527`); INFERRED MEDIUM, UNTESTED refundable ignores prior credit notes (`:461`); credit-only notes (no cash refund, `credit_amount_cents: 0` hard-coded, `:477`) not supported; reason is always "other".
- Fix: read credit note back and compare, subtract existing credit notes, add credit-note-against-AR (not cash) path.

#### 9. Accounts payable / vendor bills — PARTIAL (read-only)
- Implemented: A/P outstanding KPI from submitted Purchase Invoices and "no draft purchase invoices" close check (`apps/books/core.py:205-216,253-262`). Supplier delivery/commitment analytics (`intelligence/families/supplier_broker.py:127`).
- Gaps: CONFIRMED HIGH no bill capture/OCR, three-way match (PO/receipt/bill), approval routing, payment run, or vendor master management. No write capability for Purchase Invoice/Payment Entry. Supplier tax_id/bank change fraud checks absent (entity resolution only, `business/entity_resolution.py:13`).
- Tests: `books/test_operator.py::test_close_checklist_is_computed_not_constant` covers the A/P draft check indirectly. UNTESTED for A/P KPI.

#### 10. Expense claims / month-end close — PARTIAL
- Close: IMPLEMENTED with strong safeguards — computed readiness checklist, approval-bound exact parameters, idempotent Period Closing Voucher submit, rejects inconsistent re-close, GL read-back (`apps/books/core.py:253-275,419-505,515-575`; tests `test_operator.py::test_close_refuses_to_stage_when_checks_fail`, `::test_close_checklist_is_computed_not_constant`, `::test_close_stages_ready_when_all_checks_pass`, `::test_close_not_ready_when_erpnext_unavailable`, `::test_close_executes_verifies_idempotent_and_rejects_inconsistent`).
- Gaps: CONFIRMED HIGH naming/semantics: the action posts a fiscal-year Period Closing Voucher, i.e. an annual close that locks the year, not a monthly close; the README/dashboard say "month-end" (`books/core.py:46,515`; `deploy/module_service.py:159` "Month-end close June"). A monthly close that the user believes is reversible could lock a whole fiscal year. Fix: rename and warn, or implement period-lock/accrual-based monthly close.
- CONFIRMED MEDIUM: checklist requires "bank transactions reconciled" which no agent action can achieve (row 3), so agent-only close cannot become ready.
- CONFIRMED MEDIUM: no accruals, depreciation, FX revaluation, tax, intercompany, or variance review in the checklist.
- Expense claims: NOT_PRESENT — only a doctype name in the world adapter (`world/adapters.py:229`, title-only upsert). No receipts, policy, per-diem, approval, reimbursement.

#### 11. Subscription lifecycle + billing failures (Lago) — PARTIAL
- Implemented: create with pre-existence check and saga undo, cancel with ID validation, expansion-opportunity scan on usage vs plan (`apps/billing/core.py:543-648`, `agentic_os/integrations/lago.py:70-100`; tests `billing/test_contracts.py::test_create_subscription_requires_explicit_customer_and_plan`, `::test_undo_refuses_without_a_valid_subscription_id`, `tests/test_lago_expansion_source.py`). Failure handling: invoice `FAILED` display state (`core.py:96`), dunning retry (row 2), read-back.
- Gaps: CONFIRMED HIGH no plan change/upgrade/downgrade, pause, trial, proration, usage event ingest, coupon, renewal/notice management, involuntary-churn workflow, card-update outreach, webhook consumption (state is polled; dashboard cache 15s, `core.py:79`). CONFIRMED MEDIUM single `DEFAULT_PLAN_CODE`; no customer creation (customer must pre-exist in Lago). UNKNOWN whether Lago webhooks for `invoice.payment_failure` are configured; nothing consumes them.

#### 12. Contracts / renewals / certificates — PARTIAL (drafting only)
- Implemented: approved-template drafting with required-field validation, governance levels (L0 clerical auto, higher needs professional), professional-review request/response as governed evidence (`agentic_os/legal/{templates,drafting,governance,review}.py`; tests `tests/test_legal.py::test_draft_from_approved_template_validates`, `::test_draft_missing_field_is_invalid_not_silently_filled`, `::test_governance_levels_gate_execution`, `::test_professional_review_roundtrip_becomes_governed_decision`).
- Gaps: CONFIRMED HIGH no contract repository, renewal/expiry tracking, obligation extraction, e-signature (docusign UNSUPPORTED, `connector_capabilities.yaml:116-128`), or certificate (TLS/insurance/compliance cert) tracking as a product feature. One template (NDA-like, `templates.py:64`).
- Certificate/insurance expiry appears only as `SME_ITEMS` constants (see row 16).

#### 13. Customer onboarding / KYC — PARTIAL
- Onboarding saga across apps exists: Lago subscription + ERPNext revenue JE + consent ledger filing + support welcome with read-back (`billing/core.py:543`, `books/core.py:595`, `compliance/core.py:538`, `support/core.py:382`; tests `support/test_operator.py::test_send_onboarding_reads_back_the_message_before_claiming_sent`, `compliance/test_operator.py::test_file_consent_is_idempotent`, `books/test_operator.py::test_record_revenue_then_reverse_is_a_saga`). Note: the revenue JE debits cash/credits income at once (`books/core.py:595-633`): cash-basis, no deferred revenue, no tax.
- KYC: SIMULATED only. The world `kyc.screen` handler returns the input flag (`lambda i,c: {"sanctioned": i.get("sanctioned", False), ...}`, `agentic_os/world/worlds.py:54-55`), and the write is labelled SIMULATED (`worlds.py:~75`). A real replayable ownership-graph screen exists as a demo (`agentic_os/intelligence/temporal_graph.py:133-170`, tests `tests/test_temporal_graph.py`, `tests/test_demo_server.py::*`), using GLEIF/OpenSanctions snapshots, not customer identity verification (no ID docs, liveness, UBO collection, PEP/adverse media, ongoing monitoring).
- Test: `tests/test_world_runtime.py::test_kyc_world_go_and_no_go` (asserts the simulation).
- Fix: label KYC as vendor-ownership demo; add a connector contract for an identity-verification provider with VERIFIED_WRITE evidence before claiming KYC.

#### 14a. Privacy requests (DSAR) — IMPLEMENTED
- Evidence in app-boundaries.md section 3: intake/verify/access/delete/correct, SLA clock, hash-chained audit, strict gating, honest partial erasure. Tests: `apps/agentic-privacy/test_dsar_hardening.py` (see section 3.1 list), `test_operator.py`, `test_llm_egress.py`.
- Caveats (CONFIRMED): Lago lookup truncated at 100 customers (`core.py:438`), erasure note says anonymized but nothing anonymizes (`core.py:461`), SLA display-only (`core.py:1183`), no extension/exemption states. Billing/tax records are retained, not erased, which is the documented legal basis (`core.py:454-462`).

#### 14b. Retention — PARTIAL
- `retention_scan` dry-run by default, audited purge, covers ERPNext uncontacted Leads older than `PRIVACY_RETENTION_DAYS=730` (`core.py:1093-1127`; test `test_retention_unauth_is_counts_only_dry_run`). Gaps: single system/object, per-record delete errors swallowed (`:1117`), no per-category retention schedule, no legal hold, no evidence-retention for Compliance ledger or scan history (app-boundaries 2.4).

#### 15. Payroll — NOT_PRESENT
- CONFIRMED (HIGH marketing risk). No payroll code. The only occurrences are static mock KPIs "Payroll $61k / 12 crew + office" and a "payroll" bar in the canned dashboard (`deploy/module_service.py:153,167,177`).
- Fix: keep mock dashboards out of customer-visible builds or watermark them.

#### 16. Licensing / permits — NOT_PRESENT (demo constants only)
- Four hard-coded entries (`apps/compliance/core.py:95-100`) with an expiry-within-90-days flag (`:189-203`). No add/edit/ingest, no reminders, no renewal workflow, no evidence upload. CONFIRMED HIGH. The sample "Errors & Omissions" expiry `2026-09-08` is already past and is not labelled expired (`:193-196`). The canned dashboard also advertises "State contractor license", "OSHA fall protection training" (`deploy/module_service.py` compliance block).
- Fix: store + connector, EXPIRED state, notify via GateNotifier-like outbox.

#### 17. Support SLA / customer communication — PARTIAL
- Implemented (all approval-gated or private-note-only): draft reply as private note, send onboarding with read-back of the posted message, resolve, escalate with explicit assignee (`apps/support/core.py:284,382,484,504,534`; tests `support/test_operator.py::test_invoke_draft_reply_posts_private_note`, `::test_send_onboarding_reads_back_the_message_before_claiming_sent`, `::test_send_onboarding_with_existing_conversation_needs_no_inbox`, `::test_escalate_requires_explicit_assignee_and_writes_nothing`, `::test_gated_capability_stages_over_http_even_with_forged_approval`). Library primitives: opt-out detector, follow-up cadence/risk-radar, handoff decision (`agentic_os/support_autonomy.py:51,97,226`; tests `tests/test_support_autonomy.py`).
- Gaps: CONFIRMED HIGH SLA is a label only ("SLA 30m", `support/core.py:258`); first-response KPI is share-replied, not time-vs-target; no SLA timer, breach alert, priority policy or business-hours calendar. CONFIRMED MEDIUM the opt-out/follow-up primitives are not called by the support app (grep: only `priority_engine.py:485` references them). CONFIRMED LOW "never sleeps" marketing (`guide/core.py:24`) vs staged/approval-required sends.

#### 18. Supplier / procurement follow-up — PARTIAL (analytics only)
- Implemented: supplier reliability metrics (days late p90, confirm latency), supply impact, order lineage/blockers, entity resolution for suppliers (`agentic_os/intelligence/families/supplier*.py`, `supply*.py`, `order*.py`; tests `tests/test_family_supplier*.py`, `tests/test_family_supply*.py`, `tests/test_family_order*.py`); ERPNext PO/goods-receipt reads (`integrations/erpnext.py:135-216`).
- Gaps: CONFIRMED HIGH no outbound follow-up (no chase email/WhatsApp on a late PO, no RFQ, no PO creation/amend, no supplier portal). The design for a first real connector follow-up is a doc only: `docs/connectors/WHATSAPP_TRAVEL_VERTICAL.md`.

---

### Cross-cutting findings affecting many rows

| ID | Finding | Label | Severity | Fix |
|---|---|---|---|---|
| X1 | Marketing/Guide text claims invoicing, reconciliation, payroll-like and tax-like items that have no code (`apps/guide/core.py:22-23`, `deploy/module_service.py:153-179`) | CONFIRMED | HIGH | Generate Guide cards from the capability registry; watermark mock dashboards |
| X2 | No invoice or tax state machine (above) | CONFIRMED | HIGH | See proposed lifecycle |
| X3 | No cross-system join (Lago invoice <-> ERPNext Sales Invoice <-> bank txn <-> Chatwoot ticket); each app is its own silo with a global `TENANT` constant | CONFIRMED | HIGH | Canonical `Invoice`/`Payment` objects exist (`business/contracts.py:132-175`) but no normalizer for Lago/ERPNext feeds them into one case; add identity resolution + case aggregate |
| X4 | Approval = shared Basic credential `operator-basic` | CONFIRMED | HIGH | Signed per-person approval tokens (`docs/APPROVAL_TOKENS.md`) for money-moving and period-locking actions |
| X5 | Money in float: books P&L/cash sums use float (`books/core.py:139-168`), ERPNext rate via `round(float*100)` (`integrations/erpnext.py:27-33`); books `_cents` uses Decimal ROUND_HALF_UP (`books/core.py:105-110`) | CONFIRMED | MEDIUM | Decimal end-to-end |
| X6 | Multi-currency: refund and revenue paths check currency; receivables/materiality/quote do not | CONFIRMED | MEDIUM | Carry currency on every amount; refuse cross-currency arithmetic |
| X7 | Read caps and silent truncation: ERPNext AR 200 rows, Lago customers 100 (privacy), `_get_list` limit 0 default | CONFIRMED | MEDIUM | Paginate and report truncation |
| X8 | Connector registry lists only slack, gmail, calendar, hubspot, stripe, polar, apollo, klaviyo, postiz, ayrshare, blotato, whatsapp, web as VERIFIED; quickbooks/xero/salesforce/zendesk/docusign/twilio UNSUPPORTED (`connector_capabilities.yaml:116-128`). Lago/ERPNext/Chatwoot/Twenty are accessed by app-local clients not in this registry | CONFIRMED | MEDIUM | Register Lago/ERPNext/Chatwoot capabilities with the same status vocabulary so "VERIFIED" is comparable |
| X9 | All verification is contract/fixture-level; live lanes are opt-in (`test_erpnext_receivables_source.py::test_live_erpnext_open_receivables_read_only`, `test_erpnext_quote_source.py::test_live_erpnext_quote_feasibility`) | CONFIRMED | MEDIUM (UNTESTED live) | Run live lane against a seeded Lago+ERPNext in CI nightly |
