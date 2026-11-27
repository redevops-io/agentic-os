# Enrichment metering, CRM handoff and counterparty profiles

## Purpose

Contact enrichment (`agentic_os/world/enrichment.py`) calls paid providers. Three things were missing around
those calls: a record of what was called and what it cost, a governed way to put a result into the CRM, and
a single view of a counterparty assembled from the public registers. This document describes each.

## Metering and provenance

`agentic_os/world/enrichment_metering.py`

| Setting | Meaning |
|---|---|
| `ENRICHMENT_LEDGER_PATH` | SQLite file for the ledger. Setting it turns metering on for `get_provider()`. |
| `ENRICHMENT_TENANT` | Tenant the calls are booked to. Required when the ledger is set. |
| `ENRICHMENT_PRICE_MICROS` | JSON object, `"<provider>.<operation>": <micro-USD>`. No built-in prices. |
| `ENRICHMENT_BUDGET_MICROS` | Optional cap on known spend for the tenant. |

- Each provider call appends one ledger row: time, tenant, provider, operation, company domain, request
  digest, response digest, outcome (`match`, `no_match`, `error`, `refused_budget`) and cost. The ledger is
  append-only; `UPDATE` and `DELETE` are rejected by the database.
- A call with no configured price is booked with cost `unknown`. `summary()` reports known spend and the
  count of unknown-cost calls separately.
- With a budget set, a call is refused before the provider is contacted when the price is unknown or would
  exceed the remaining budget.
- The ledger keeps the company domain in clear text. Person names and addresses appear only inside digests.
- Results from a metered provider carry a `provenance` list (provider, operation, retrieval time, digests,
  ledger entry, cost basis). `resolve_verified_email` and `resolve_contact` pass it through.

## Approval-gated CRM handoff

`agentic_os/integrations/twenty_operator.py` adds two capabilities next to `crm.contact.upsert`:

- `crm.company.upsert` — one Company per domain.
- `crm.enrichment.handoff` — writes an enriched contact and its company as a linked pair.

Both are `side_effecting` and `approval_required`: an invoke without a runtime-delivered approval returns a
staged preview and writes nothing. `TWENTY_WRITE_SHADOW_MODE` (default on) additionally keeps an approved
call from reaching Twenty.

The handoff refuses, before any write, a contact with no provenance, an unverified address, or an address
outside the company's domain (unless the approver sets `allow_domain_mismatch`).

Company matching compares normalized hosts (`https://www.acme.com/` and `acme.com` are the same company). A
failed lookup returns an error instead of creating a record.

## Counterparty profile

`agentic_os/intelligence/counterparty.py` — capability `counterparty.profile` (read-only).

It composes the existing register adapters: GLEIF (keyless), OpenSanctions (`OPENSANCTIONS_API_KEY` or a
self-hosted `OPENSANCTIONS_BASE_URL`), and optionally Companies House, OpenCorporates, SEC EDGAR and Open
Ownership when their settings are present.

Each section (`identity`, `sanctions`, `ownership`) reports `found`, `no_match`, `ambiguous`, `not_checked`
or `unavailable`, with the sources it rests on. The overall `assessment` is:

| Value | When |
|---|---|
| `sanctions_review_required` | A screening result scored at or above 0.7. Reported for review, not adjudicated. |
| `incomplete` | Identity or sanctions was not checked or the provider failed. |
| `identity_unresolved` | Screening ran, but no single register entry carries the queried legal name. |
| `no_adverse_findings` | Identity resolved to one entity and screening ran with nothing to review. |

`coverage_gaps` lists every section that did not run and why.

## Tests

- `tests/test_enrichment_metering.py`, `tests/test_counterparty.py`, `tests/test_twenty_enrichment_handoff.py`
- `tests/test_twenty_live_handoff.py` runs against a real Twenty when `TWENTY_LIVE_TEST_URL` and
  `TWENTY_LIVE_TEST_KEY` name a disposable workspace. It creates records.
