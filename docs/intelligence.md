# External & Professional Intelligence

The incumbent moats behind CRM/ERP/BI/security tools are mostly *data network* moats — Apollo's contact graph,
Klaviyo's cross-customer benchmarks, D&B's entity hierarchy, a mature deliverability reputation. ReDevOps does not
try to reproduce those. Instead the apps sit on a first-class **intelligence provider layer** behind the Discovery
Runtime: an app asks for a **capability**, a gate decides whether the evidence can change the decision and is worth
its cost, an adapter acquires it, and the result is a governed artifact carrying provenance / cost / freshness /
license. Later, verified-outcome accounting says whether buying it actually helped.

> The rule the whole layer enforces: **apps ask for a capability, never a provider.** There is no `Twenty → Apollo`
> call site anywhere. `Twenty` asks for `PERSON_ENRICHMENT`; Discovery decides whether to acquire it and which
> entitled provider serves it.

The contract is domain-neutral and lives in [`runtime-contracts`](https://github.com/redevops-io/runtime-contracts)
(`protocol/intelligence.py`, `protocol/email_delivery.py`); the adapters, gate wiring, accounting, and per-app
entitlement live here in [`agentic_os/intelligence/`](../agentic_os/intelligence/) and
[`agentic_os/legal/`](../agentic_os/legal/).

## The acquisition loop

```text
app names a capability            (agentic_os/intelligence/apps.py — request_for)
  → gate: entitled? PII-purpose ok? within budget? can it change the action?   (§8)
  → acquire from the cheapest entitled provider, with a fallback ladder        (§11)
  → governed EvidenceArtifact (provenance/cost/freshness/license)              (§4.2)
  → record the lookup in the evidence-value ledger                             (§8/WP8)
  → later: backfill the verified outcome, and evaluate the provider            (§9)
```

The single seam an app/DecisionCase uses is
[`discovery_bridge.acquire_for_decision`](../agentic_os/intelligence/discovery_bridge.py) — it runs the gate, drives
the ladder, and appends to the ledger.

## Capabilities

Defined once in `runtime_contracts.protocol.Capability`. An app requests one of these; it never sees a provider name.

| Group | Capabilities |
|---|---|
| Sales / entity | `PERSON_SEARCH`, `PERSON_ENRICHMENT`, `COMPANY_ENRICHMENT`, `COMPANY_IDENTITY`, `CORPORATE_HIERARCHY`, `BENEFICIAL_OWNERSHIP`, `SANCTIONS_RISK` |
| Payments | `PAYMENT_FRAUD_SCORE` |
| Market / web | `WEB_TRAFFIC_INTELLIGENCE`, `SEARCH_KEYWORD_INTELLIGENCE`, `BACKLINK_INTELLIGENCE`, `SOCIAL_LISTENING` |
| Security | `DOMAIN_REPUTATION`, `IP_REPUTATION`, `PASSIVE_DNS`, `MALWARE_REPUTATION`, `THREAT_INTELLIGENCE`, `VULNERABILITY_EXPLOITABILITY` |
| Marketing infra | `EMAIL_DELIVERY` (execution — see below) |
| Legal (Professional Intelligence) | `LEGAL_RESEARCH`, `LEGAL_AUTHORITY_LOOKUP`, `LEGAL_DOCUMENT_DRAFT`, `LEGAL_DOCUMENT_REVIEW`, `LEGAL_CLAUSE_REVIEW`, `LEGAL_PRECEDENT_LOOKUP`, `LEGAL_JUDGMENT_REQUIRED` |

## Providers: open baseline + Bring-Your-Own

The open stack runs with **no paid keys** (§12 Bring-Your-Own-Intelligence). Paid and professional providers register
on top only when the tenant supplies a credential — an unentitled provider is simply not registered, so the open
baseline is never affected.

| Family | Providers | Notes |
|---|---|---|
| Open baseline | GLEIF, OpenSanctions (open `yente` or keyed hosted), OpenCorporates (BYO token) | `default_registry()` |
| Paid GTM / entity | Apollo, Similarweb, Semrush, D&B Direct+, Brandwatch | `register_paid_providers(...)` |
| Payments | Stripe · Radar | `PAYMENT_FRAUD_SCORE` from charge outcome |
| Security TI | Cloudflare TI (needs `account_id`), VirusTotal (**enforces** a commercial license — refuses the free API), Microsoft Defender TI | |
| Legal (Professional) | LexisNexis, Thomson Reuters CoCounsel | preserve authority/jurisdiction/citations |
| Email delivery (execution) | Postmark, Amazon SES | see [Email delivery](#email-delivery-execution) |
| P2 entity / risk (on demand) | ZoomInfo, LSEG Risk Intelligence, LexisNexis Risk Solutions, Moody's | `register_p2_providers(...)`; screening providers treat a clean screen as evidence |
| P2 vuln scanners (on demand) | Tenable, Qualys, Rapid7 | customer-owned scanners feeding `VULNERABILITY_EXPLOITABILITY` (+ threat intel) |

**P2 providers are on-demand** (§6 P2): they're built ready-to-wire but registered only when a pilot/customer
supplies credentials, via `register_p2_providers`. They reuse existing capabilities, so a customer's own scanner or
screening feed competes on cost/value in the evidence-value ledger like any other provider. **Not built:** tax
determination and bank/account-aggregation providers — they'd need new contract capabilities and are determination /
highly-sensitive-flavored, so per §6 P2 they wait for a concrete customer evidence requirement (as `EMAIL_DELIVERY`
did before its contract).

Wiring paid providers is one call:

```python
from agentic_os.intelligence import default_registry, register_paid_providers
reg = default_registry(opencorporates_token=os.environ.get("OPENCORPORATES_TOKEN", ""))
register_paid_providers(reg, apollo_key=os.environ.get("APOLLO_KEY", ""),
                        stripe_key=os.environ.get("STRIPE_KEY", ""))   # blanks are skipped
```

Every adapter takes an injectable `fetch` seam, so the whole layer is offline-testable with no network.

## Per-app entitlement

[`apps.py`](../agentic_os/intelligence/apps.py) expresses the provider-to-app matrix (§7) as **capability
entitlement**, not hard-wired call sites. `request_for(app, capability, …)` builds a governed `EvidenceRequest`
tagged with the app's declared purpose (the PII-purpose gate reads it) and derived sensitivity, and **refuses** a
capability outside the app's remit.

```python
from runtime_contracts.protocol import Capability
from agentic_os.intelligence import request_for

req = request_for("twenty", Capability.PERSON_ENRICHMENT,
                  decision_case_id="dc-101", subject_refs=("jane@acme.com",), tenant="summit")
# → purpose="crm contact enrichment", sensitivity=PII
# request_for("umami", Capability.PAYMENT_FRAUD_SCORE, ...) raises — outside Umami's remit.
```

Entitled apps: `twenty`, `erpnext`, `metabase`, `lago`, `postiz`, `umami`, `changedetection`, `crowdsec`,
`openscap`, `listmonk`, `entity_risk`.

## Evidence-value accounting & the evaluation harness (§8/§9)

Every gated lookup appends an `EvidenceValueRecord` (why requested, provider, cost, decision before/after, action,
and later the verified outcome) to the [`EvidenceValueStore`](../agentic_os/intelligence/value_store.py). Once
outcomes are backfilled, the [`evaluation`](../agentic_os/intelligence/evaluation.py) harness turns the ledger into
the §9 **paid-evidence rule** — cost per acquired evidence, per changed decision, and per *verified beneficial*
changed decision — and a verdict per `(provider, capability)`:

- **RETAIN** — verified beneficial impact at acceptable cost
- **REVIEW** — beneficial but too expensive per beneficial decision
- **DROP** — cost with no verified beneficial impact
- **INSUFFICIENT_DATA** — below the minimum verified outcomes (don't optimize routing until enough data, WP8)
- **NOT_APPLICABLE** — gate-skips and imports (nothing was purchased to judge)

Run the end-to-end demo (offline, no keys):

```bash
PYTHONPATH=/path/to/runtime-contracts python examples/intelligence_moat_demo.py
```

## Historical outcome migration (WP7)

When a customer leaves Zendesk / Intercom / Klaviyo, [`imports.py`](../agentic_os/intelligence/imports.py) preserves
their prior **Experience** rather than feature parity. `from_zendesk` / `from_intercom` / `from_klaviyo` normalize
customer-owned exports into `HistoricalOutcome` (beneficial/adverse/neutral), and `seed_value_store` writes them as
`provider=import:<source>`, cost 0, `evidence_requested=False` — so imports seed the outcome corpus without
distorting the paid metrics.

## Legal (Professional Intelligence, §19)

[`agentic_os/legal/`](../agentic_os/legal/) is **not** an "AI lawyer": it reduces professional attention on routine
assembly + evidence gathering and escalates judgment to qualified humans.

- **Drafting** — `draft_document` renders from **organization-approved** templates only; only `L0_CLERICAL`
  auto-executes.
- **Governance** — `legal_action_decision(level, …)` gates **external execution**, not evidence acquisition:
  L0 auto · L1 human · L2 specialist evidence + human · L3 professional review (`LEGAL_JUDGMENT_REQUIRED`) ·
  L4 professional authorization. Evidence never grants execution.
- **Professional review** — `ProfessionalReviewRequest` / `Decision` is a governed handoff; an approved decision
  satisfies the L3/L4 gate.
- **Specialist providers** — LexisNexis / CoCounsel preserve authority; a generated proposition with no authority is
  **not** authoritative.

## Email delivery (execution)

Email delivery is something the Runtime *does*, not something it *learns*, so it has its own contract
(`protocol/email_delivery.py`) and bridge ([`intelligence/email/`](../agentic_os/intelligence/email/)) — not the
acquisition gate. `send_email` enforces **suppression** (refuses a suppressed recipient without calling the
provider), **idempotency** (a delivered key is not re-sent), records every receipt to a ledger, and updates the
suppression list when a receipt is a bounce / complaint / unsubscribe. Providers (Postmark, SES) are BYO, so
Listmonk's own sending is unchanged when no credential is supplied.

```python
from agentic_os.intelligence.email import PostmarkProvider, ReceiptStore, SuppressionList, send_email
receipt = send_email(PostmarkProvider(credential=os.environ["POSTMARK_TOKEN"]), request,
                     receipts=ReceiptStore("receipts.jsonl"), suppression=SuppressionList("suppression.jsonl"))
```

## What not to build

Per the plan (§16): don't reimplement incumbent data networks, don't add one-off API calls inside apps, and don't
assume more external context improves a decision — the gate exists precisely because it often does not.
