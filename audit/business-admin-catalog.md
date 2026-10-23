# Business-admin capability catalog audit (workstream D) — 2026-10-23

Status vocabulary: IMPLEMENTED (product code does it, with tests) / PARTIAL (some of the row, gaps named) / CONNECTOR_ONLY (the underlying OSS core does it; AOS adds no agent capability) / NOT_PRESENT / UNKNOWN.
Finding labels: CONFIRMED (read in code) / INFERRED (follows from code, needs live proof) / UNTESTED. Severity: CRITICAL/HIGH/MEDIUM/LOW.
Method: static read; tests were NOT executed. "Test" = test exists by that name; I did not run it.
Paths relative to `/Users/arybach/agentic-os`. Product capability and README/marketing claim are shown separately.

Key marketing sources: Guide card text `apps/guide/core.py:22-37` (what users are told), app READMEs (`apps/billing/README.md`, `apps/books/README.md`, `apps/compliance/README.md`), and canned demo `deploy/module_service.py` (explicitly "mock data", `deploy/module_service.py:1-12`).

## Summary

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

## State machines and LLM arithmetic (asked separately)

### Invoice state machine DRAFT->VALIDATED->APPROVED->ISSUED->DELIVERED->PAID
NOT_PRESENT (CONFIRMED, HIGH). Searched `TRANSITIONS`, `ALLOWED_TRANSITION`, `can_transition`, `StateMachine`, `VALIDATED`, `ISSUED` across `agentic_os/` and `apps/`: no hits. What exists instead:
- `Invoice.status` is a free-text field with a comment "draft | open | paid | void | uncollectible" (`agentic_os/integrations/business/contracts.py:149`). No guard, no transition table, no frozen-history check (objects are frozen dataclasses, so a new observation is a new object, not a validated transition).
- Billing display collapse: Lago status/payment_status/overdue -> DRAFT/SENT/PAID/OVERDUE/FAILED label (`apps/billing/core.py:86-100`). Read-only presentation; VALIDATED, APPROVED, DELIVERED do not exist. Lago itself owns draft->finalized.
- Collection lifecycle `identified -> actioned -> promised -> paid -> verified` (`agentic_os/commercial/receivables.py:90-100`) and order chain `received -> sourced -> reserved -> fulfilled -> invoiced -> verified` (`agentic_os/commercial/order.py:17,42-53`) are `ExpectedTransition` monitors (missed/overdue/diverged detection, `agentic_os/reliability.py:59-90`). They detect lateness; they do not enforce legal moves. Tests: `tests/test_reliability_order.py` (5), `tests/test_commercial_receivables.py::test_collection_lifecycle_transitions`.
Proposed fix: add a pure `InvoiceLifecycle` transition table (DRAFT->VALIDATED requires totals recompute + customer tax id present; VALIDATED->APPROVED requires a human approval token bound to the content digest; APPROVED->ISSUED requires connector write + read-back; ISSUED->DELIVERED requires dispatch receipt; ->PAID requires payment match), reuse `ObligationEngine` for the ISSUED/DELIVERED proofs, and make illegal transitions raise.

### Tax-obligation state machine OBSERVED->EVIDENCE_REQUIRED->REVIEW_REQUIRED->SUBMITTED->ACCEPTED
NOT_PRESENT (CONFIRMED, HIGH). No tax obligation object exists. Grep for `vat|gst|sales tax|tax return|filing|e-invoic` returns only: SEC EDGAR filing presence for counterparties (`agentic_os/intelligence/adapters/sec_edgar.py`), `tax_id` as an entity-resolution key (`agentic_os/intelligence/families/supplier_resolution.py:23`), the legal-governance label "L4 filing/representation -> professional authorization" (`agentic_os/legal/governance.py:29`), and the consent-ledger "filing" of a GDPR consent record (`apps/compliance/core.py:538`). The nearest reusable machinery: `legal/governance.py` level gate and `legal/review.py` professional-review handoff (`ProfessionalReviewRequest`, `review.py:15`) that returns governed evidence.
Fix: model `TaxObligation{jurisdiction, period, kind, due, state, evidence_refs}` over `ExpectedTransition`, with SUBMITTED gated at the L4 professional-authorization level that already exists.

### Can an LLM compute tax or totals?
No path found. CONFIRMED (grep of every `governed_text`/`.complete(` call site in `agentic_os/` and `apps/`).
- Billing/Books/Edge Sentinel/Growth/Market: LLM output is a one-sentence narration passed back as `reasoning` (`apps/billing/core.py:226-232`, `apps/books/core.py:353-358`). Amounts in the prompts come from deterministic fields.
- Planner: outcomes/needs/dependencies only, no numeric inputs (`agentic_os/mission/planner.py:107-139`).
- Assistants: tools are keyword-routed; fallback LLM answers from a static primer only (`apps/billing/assistant.py:66-84`).
- Quote totals: integer math from catalog price (`agentic_os/revenue/quote.py:117-120`).
- Residual (INFERRED, LOW): outreach/lifecycle LLM drafts (`apps/outreach-engine/app.py:162,404`, `apps/lifecycle/core.py:134,201,227`) may state prices or dates in free text; no check that figures in drafts match source data. Fix: template numeric fields, validate drafts against a fact sheet before approval.
- Residual (INFERRED, LOW): approver sees LLM prose next to the staged action; the structured fields are the authoritative ones. Show structured diff first.

---

## Row details

### 1. Invoice creation + dispatch — CONNECTOR_ONLY
- Product: agentic-billing wraps Lago. Invoices are produced by Lago as a side effect of subscriptions: `create_subscription` (`apps/billing/core.py:543-606`, gated at `:561`, idempotent by `external_id`, saga undo `cancel_subscription` `:607`). There is no capability to create, edit, validate, finalize, email or download an invoice. Billing operator capabilities are exactly `billing.summary/dunning/refund/create_subscription/cancel_subscription` (`apps/billing/operator.py:71-114`).
- ERPNext side: Books writes only Journal Entry (`books/core.py:595 record_revenue`) and Period Closing Voucher (`:452`). The world adapter would upsert a `Sales Invoice` with payload `{"title": label}` only (`agentic_os/world/adapters.py:224-232,263`); ERPNext requires customer/items/dates, so INFERRED (MEDIUM, UNTESTED against live) it fails and surfaces as `CoreUnavailable`.
- Dispatch: only through the generic gmail/slack send capabilities (VERIFIED by contract, `connector_capabilities.yaml:30-34`) used for follow-ups (`business/missions.py:139`), not for invoice delivery.
- README claim: Guide card says "Checkout -> invoicing -> reconciliation -> dunning" with actions "checkout", "reconciliation" (`apps/guide/core.py:22`). CONFIRMED over-claim (HIGH): `grep checkout apps/billing` finds nothing; billing has no reconciliation capability.
- Tests (adjacent): `apps/billing/test_contracts.py::test_create_subscription_result_passes_verifier`, `::test_forward_verify_undo_round_trip_targets_the_created_subscription`, `::test_undo_never_cancels_a_subscription_that_already_existed`.
- Fix: either remove "checkout"/"reconciliation"/"invoicing" from marketing text, or add `billing.invoice.finalize` / `invoice.send` with read-back; add Lago invoice dispatch proof via ObligationEngine.

### 2. Overdue receivables / collections — PARTIAL
- Implemented: scoped dunning = Lago `retry_payment` then per-invoice read-back, `dunning_verified` only if all settle, partial never reported done (`apps/billing/core.py:289-358`; tests `test_closed_loop.py::test_full_chain_happy_path`, `::test_partial_settlement_fails_the_node_and_is_reported_partial`, `::test_rejected_retry_or_nothing_settled_fails_the_node`, `::test_operator_scoping_only_chases_the_requested_invoices`, `::test_no_approval_executes_nothing`). Decision cycle with abstain and outcome attribution (`apps/billing/decision.py`, `test_loop_durability.py`).
- Cross-system assessment (dispute/delivery/support/promise priority) exists as a pure function (`agentic_os/commercial/receivables.py:63-90`, tests `test_commercial_receivables.py` x6) and an exact-content-bound email follow-up draft/send/verify mission (`agentic_os/integrations/business/missions.py:95-158`, tests `test_integration_business_receivables.py` x6).
- Gaps:
  - CONFIRMED HIGH: "dunning" = payment retry on the card/payment provider, not a collections communication sequence. No reminder cadence, dunning levels, late fees, payment plans, write-off, or promise-to-pay tracking.
  - CONFIRMED HIGH: legacy `chase_overdue` always returns `status: "done"` and "dunning reminder sent" regardless of the HTTP result, no read-back (`billing/core.py:201-258`); the dashboard action uses it (`billing/app.py:344-346`). False success on the user-facing path. Fix: delegate to `chase_invoices`.
  - CONFIRMED HIGH: live ERPNext feed forces `delivery_state=DELIVERED` and no dispute/support state (`commercial/receivables_sources.py:65`), so every overdue invoice becomes COLLECT; the README "reason across delivery/dispute/support" claim holds only for the pure function.
  - CONFIRMED MEDIUM: ERPNext AR read capped at 200 rows, unordered (`integrations/erpnext.py:109`); mixed-currency materiality (`business/missions.py:124`).
  - CONFIRMED MEDIUM: two disjoint collection paths (Lago retry vs email follow-up) never meet; no shared receivable case record.

### 3. Payment matching + reconciliation — PARTIAL
- Implemented: payout invariant engine with tolerance and unknown-type surfacing (`agentic_os/integration/reconciliation.py:47-95`; tests `tests/test_experiment_payout_reconciliation.py` x7); ERPNext bank-transaction vs A/R exact-cents proposal (`apps/books/core.py:379-400`; tests `books/test_contracts.py::test_reconcile_compares_to_the_cent`, `books/test_operator.py::test_reconcile_matches_deposit_to_invoice`).
- Gaps:
  - CONFIRMED HIGH: Books reconcile is first-invoice x first-deposit with equal cents; no party/reference/currency/date, no partial, split or multi-invoice matching, no confidence, no ambiguity handling (two equal-amount invoices -> arbitrary pick). Stages a proposal only; there is no posting capability (no Payment Entry write anywhere; `_DOCTYPE` maps "payment" -> Payment Entry only in the world adapter, with title-only payload, `world/adapters.py:228`).
  - CONFIRMED MEDIUM: summary text says "staged for posting" (`books/core.py:~408`) but nothing stages or posts.
  - CONFIRMED MEDIUM: payout reconciliation is under `integration/experiments/` and not wired into any app (grep for imports: only experiments/testkit).
  - CONFIRMED MEDIUM: Lago payments are never matched to ERPNext receipts; two ledgers remain unlinked.
  - CONFIRMED LOW: `categorize` is a `reference_number` tag with category chosen by direction only (`books/core.py:325`); labelled `mode: tag_only`.
- Fix: score candidates on (party, reference, amount, currency, date window), return top-N with ambiguity flag, add a governed `books.post_payment_entry` with GL read-back, wire the payout invariant to Lago/Stripe settlement data.

### 4. Quote -> order -> delivery -> invoice — PARTIAL
- Implemented: deterministic quote feasibility with availability, promised date, margin floor and approval thresholds (`agentic_os/revenue/quote.py:85-153`; tests `tests/test_revenue_quote.py` x6, `tests/test_erpnext_quote_source.py` x6 incl. live-optional); canonical `SalesOrder`, `SalesOrderLine`, `Shipment`, `PurchaseOrder`, `GoodsReceipt` objects (`business/order.py`, `business/supply.py`); order-stage monitor received..verified (`commercial/order.py`, tests `test_reliability_order.py`); unanswered-quote leakage from Chatwoot (`integrations/chatwoot.py:83`).
- Gaps: CONFIRMED HIGH no quote creation in ERPNext, no quote->sales order conversion, no delivery note, no sales-invoice-from-order; the chain is monitored not driven. `evaluate_order` is read-only intelligence (`commercial/bindings.py:40-45`). CONFIRMED MEDIUM quote total is tax-exclusive and currency is a caller-supplied default `"USD"` not checked against catalog (`quote.py:85-89`); unit price `round()` is banker's rounding and line total truncates `int(unit_price * qty)` with float qty (`quote.py:116-117`), INFERRED LOW rounding drift on fractional quantities.
- Fix: governed `erp.quote.create` / `order.create` / `invoice.create_from_order` with read-back, Decimal arithmetic, currency and tax in the quote contract.

### 5. VAT/GST/sales-tax registration + thresholds — NOT_PRESENT
- CONFIRMED (HIGH). No registration, nexus, or threshold model anywhere (grep above). Only demo fixture: static row "Sales tax accrual / Q2 estimate / on track" in the canned mock dashboard (`deploy/module_service.py:179`), which is explicitly mock data (`:1-12`) — presents a tax status that no code computes.
- Fix: do not ship the fixture row in any tenant-visible build; add a jurisdiction registry only when a tax engine/connector exists.

### 6. Tax calculation + exemptions — NOT_PRESENT
- CONFIRMED (HIGH). No tax rate, tax id validation, exemption certificate, or tax line anywhere. `InvoiceLine`/`Invoice` have no tax fields (`business/contracts.py:132-153`). Quote totals are pre-tax (`revenue/quote.py:117-136`). Lago and ERPNext both compute tax natively; AOS neither reads nor validates it. UNKNOWN whether the tenant's Lago/ERPNext tax config is correct — nothing inspects it.
- Fix: read-only tax-line extraction + check that invoice total == sum(lines)+tax from the core; never compute tax in the agent; use a certified engine/connector.

### 7. Periodic tax return prep / filing status — NOT_PRESENT
- CONFIRMED (HIGH). No return, period, or filing object. See "tax-obligation state machine" above. Closest: PCV period close for ERPNext fiscal year (row 10) and the professional-authorization L4 gate in legal governance (`legal/governance.py:29`).

### 8. E-invoicing / credit notes — PARTIAL
- Credit notes: IMPLEMENTED for refunds — creates a Lago credit note, refund amount capped at refundable fee total, currency must match, exact cents, approval-gated (`apps/billing/core.py:428-483`, `:486-527`; tests `test_refund_and_forgery.py::test_credit_note_equals_requested_amount_or_cap`, `::test_over_request_is_rejected_without_a_credit_note`, `test_contracts.py::test_refund_currency_must_match_the_invoice`, `::test_refund_is_refused_when_the_invoice_currency_is_unknown`, `::test_absurd_refund_amounts_are_rejected_not_raised`). Stripe/Polar refund executors are contract-verified connectors (`connector_capabilities.yaml:46-56`).
- E-invoicing (Peppol/ZATCA/Facturae/clearance): NOT_PRESENT (CONFIRMED HIGH, no code).
- Gaps: CONFIRMED MEDIUM refund result taken from the POST response, no read-back of the credit note (`billing/core.py:520-527`); INFERRED MEDIUM, UNTESTED refundable ignores prior credit notes (`:461`); credit-only notes (no cash refund, `credit_amount_cents: 0` hard-coded, `:477`) not supported; reason is always "other".
- Fix: read credit note back and compare, subtract existing credit notes, add credit-note-against-AR (not cash) path.

### 9. Accounts payable / vendor bills — PARTIAL (read-only)
- Implemented: A/P outstanding KPI from submitted Purchase Invoices and "no draft purchase invoices" close check (`apps/books/core.py:205-216,253-262`). Supplier delivery/commitment analytics (`intelligence/families/supplier_broker.py:127`).
- Gaps: CONFIRMED HIGH no bill capture/OCR, three-way match (PO/receipt/bill), approval routing, payment run, or vendor master management. No write capability for Purchase Invoice/Payment Entry. Supplier tax_id/bank change fraud checks absent (entity resolution only, `business/entity_resolution.py:13`).
- Tests: `books/test_operator.py::test_close_checklist_is_computed_not_constant` covers the A/P draft check indirectly. UNTESTED for A/P KPI.

### 10. Expense claims / month-end close — PARTIAL
- Close: IMPLEMENTED with strong safeguards — computed readiness checklist, approval-bound exact parameters, idempotent Period Closing Voucher submit, rejects inconsistent re-close, GL read-back (`apps/books/core.py:253-275,419-505,515-575`; tests `test_operator.py::test_close_refuses_to_stage_when_checks_fail`, `::test_close_checklist_is_computed_not_constant`, `::test_close_stages_ready_when_all_checks_pass`, `::test_close_not_ready_when_erpnext_unavailable`, `::test_close_executes_verifies_idempotent_and_rejects_inconsistent`).
- Gaps: CONFIRMED HIGH naming/semantics: the action posts a fiscal-year Period Closing Voucher, i.e. an annual close that locks the year, not a monthly close; the README/dashboard say "month-end" (`books/core.py:46,515`; `deploy/module_service.py:159` "Month-end close June"). A monthly close that the user believes is reversible could lock a whole fiscal year. Fix: rename and warn, or implement period-lock/accrual-based monthly close.
- CONFIRMED MEDIUM: checklist requires "bank transactions reconciled" which no agent action can achieve (row 3), so agent-only close cannot become ready.
- CONFIRMED MEDIUM: no accruals, depreciation, FX revaluation, tax, intercompany, or variance review in the checklist.
- Expense claims: NOT_PRESENT — only a doctype name in the world adapter (`world/adapters.py:229`, title-only upsert). No receipts, policy, per-diem, approval, reimbursement.

### 11. Subscription lifecycle + billing failures (Lago) — PARTIAL
- Implemented: create with pre-existence check and saga undo, cancel with ID validation, expansion-opportunity scan on usage vs plan (`apps/billing/core.py:543-648`, `agentic_os/integrations/lago.py:70-100`; tests `billing/test_contracts.py::test_create_subscription_requires_explicit_customer_and_plan`, `::test_undo_refuses_without_a_valid_subscription_id`, `tests/test_lago_expansion_source.py`). Failure handling: invoice `FAILED` display state (`core.py:96`), dunning retry (row 2), read-back.
- Gaps: CONFIRMED HIGH no plan change/upgrade/downgrade, pause, trial, proration, usage event ingest, coupon, renewal/notice management, involuntary-churn workflow, card-update outreach, webhook consumption (state is polled; dashboard cache 15s, `core.py:79`). CONFIRMED MEDIUM single `DEFAULT_PLAN_CODE`; no customer creation (customer must pre-exist in Lago). UNKNOWN whether Lago webhooks for `invoice.payment_failure` are configured; nothing consumes them.

### 12. Contracts / renewals / certificates — PARTIAL (drafting only)
- Implemented: approved-template drafting with required-field validation, governance levels (L0 clerical auto, higher needs professional), professional-review request/response as governed evidence (`agentic_os/legal/{templates,drafting,governance,review}.py`; tests `tests/test_legal.py::test_draft_from_approved_template_validates`, `::test_draft_missing_field_is_invalid_not_silently_filled`, `::test_governance_levels_gate_execution`, `::test_professional_review_roundtrip_becomes_governed_decision`).
- Gaps: CONFIRMED HIGH no contract repository, renewal/expiry tracking, obligation extraction, e-signature (docusign UNSUPPORTED, `connector_capabilities.yaml:116-128`), or certificate (TLS/insurance/compliance cert) tracking as a product feature. One template (NDA-like, `templates.py:64`).
- Certificate/insurance expiry appears only as `SME_ITEMS` constants (see row 16).

### 13. Customer onboarding / KYC — PARTIAL
- Onboarding saga across apps exists: Lago subscription + ERPNext revenue JE + consent ledger filing + support welcome with read-back (`billing/core.py:543`, `books/core.py:595`, `compliance/core.py:538`, `support/core.py:382`; tests `support/test_operator.py::test_send_onboarding_reads_back_the_message_before_claiming_sent`, `compliance/test_operator.py::test_file_consent_is_idempotent`, `books/test_operator.py::test_record_revenue_then_reverse_is_a_saga`). Note: the revenue JE debits cash/credits income at once (`books/core.py:595-633`): cash-basis, no deferred revenue, no tax.
- KYC: SIMULATED only. The world `kyc.screen` handler returns the input flag (`lambda i,c: {"sanctioned": i.get("sanctioned", False), ...}`, `agentic_os/world/worlds.py:54-55`), and the write is labelled SIMULATED (`worlds.py:~75`). A real replayable ownership-graph screen exists as a demo (`agentic_os/intelligence/temporal_graph.py:133-170`, tests `tests/test_temporal_graph.py`, `tests/test_demo_server.py::*`), using GLEIF/OpenSanctions snapshots, not customer identity verification (no ID docs, liveness, UBO collection, PEP/adverse media, ongoing monitoring).
- Test: `tests/test_world_runtime.py::test_kyc_world_go_and_no_go` (asserts the simulation).
- Fix: label KYC as vendor-ownership demo; add a connector contract for an identity-verification provider with VERIFIED_WRITE evidence before claiming KYC.

### 14a. Privacy requests (DSAR) — IMPLEMENTED
- Evidence in app-boundaries.md section 3: intake/verify/access/delete/correct, SLA clock, hash-chained audit, strict gating, honest partial erasure. Tests: `apps/agentic-privacy/test_dsar_hardening.py` (see section 3.1 list), `test_operator.py`, `test_llm_egress.py`.
- Caveats (CONFIRMED): Lago lookup truncated at 100 customers (`core.py:438`), erasure note says anonymized but nothing anonymizes (`core.py:461`), SLA display-only (`core.py:1183`), no extension/exemption states. Billing/tax records are retained, not erased, which is the documented legal basis (`core.py:454-462`).

### 14b. Retention — PARTIAL
- `retention_scan` dry-run by default, audited purge, covers ERPNext uncontacted Leads older than `PRIVACY_RETENTION_DAYS=730` (`core.py:1093-1127`; test `test_retention_unauth_is_counts_only_dry_run`). Gaps: single system/object, per-record delete errors swallowed (`:1117`), no per-category retention schedule, no legal hold, no evidence-retention for Compliance ledger or scan history (app-boundaries 2.4).

### 15. Payroll — NOT_PRESENT
- CONFIRMED (HIGH marketing risk). No payroll code. The only occurrences are static mock KPIs "Payroll $61k / 12 crew + office" and a "payroll" bar in the canned dashboard (`deploy/module_service.py:153,167,177`).
- Fix: keep mock dashboards out of customer-visible builds or watermark them.

### 16. Licensing / permits — NOT_PRESENT (demo constants only)
- Four hard-coded entries (`apps/compliance/core.py:95-100`) with an expiry-within-90-days flag (`:189-203`). No add/edit/ingest, no reminders, no renewal workflow, no evidence upload. CONFIRMED HIGH. The sample "Errors & Omissions" expiry `2026-09-08` is already past and is not labelled expired (`:193-196`). The canned dashboard also advertises "State contractor license", "OSHA fall protection training" (`deploy/module_service.py` compliance block).
- Fix: store + connector, EXPIRED state, notify via GateNotifier-like outbox.

### 17. Support SLA / customer communication — PARTIAL
- Implemented (all approval-gated or private-note-only): draft reply as private note, send onboarding with read-back of the posted message, resolve, escalate with explicit assignee (`apps/support/core.py:284,382,484,504,534`; tests `support/test_operator.py::test_invoke_draft_reply_posts_private_note`, `::test_send_onboarding_reads_back_the_message_before_claiming_sent`, `::test_send_onboarding_with_existing_conversation_needs_no_inbox`, `::test_escalate_requires_explicit_assignee_and_writes_nothing`, `::test_gated_capability_stages_over_http_even_with_forged_approval`). Library primitives: opt-out detector, follow-up cadence/risk-radar, handoff decision (`agentic_os/support_autonomy.py:51,97,226`; tests `tests/test_support_autonomy.py`).
- Gaps: CONFIRMED HIGH SLA is a label only ("SLA 30m", `support/core.py:258`); first-response KPI is share-replied, not time-vs-target; no SLA timer, breach alert, priority policy or business-hours calendar. CONFIRMED MEDIUM the opt-out/follow-up primitives are not called by the support app (grep: only `priority_engine.py:485` references them). CONFIRMED LOW "never sleeps" marketing (`guide/core.py:24`) vs staged/approval-required sends.

### 18. Supplier / procurement follow-up — PARTIAL (analytics only)
- Implemented: supplier reliability metrics (days late p90, confirm latency), supply impact, order lineage/blockers, entity resolution for suppliers (`agentic_os/intelligence/families/supplier*.py`, `supply*.py`, `order*.py`; tests `tests/test_family_supplier*.py`, `tests/test_family_supply*.py`, `tests/test_family_order*.py`); ERPNext PO/goods-receipt reads (`integrations/erpnext.py:135-216`).
- Gaps: CONFIRMED HIGH no outbound follow-up (no chase email/WhatsApp on a late PO, no RFQ, no PO creation/amend, no supplier portal). The design for a first real connector follow-up is a doc only: `docs/connectors/WHATSAPP_TRAVEL_VERTICAL.md`.

---

## Cross-cutting findings affecting many rows

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
