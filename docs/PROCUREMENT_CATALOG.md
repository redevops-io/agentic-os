# Procurement catalog and RFQ

## Purpose

Let a buyer search what it can source, compare offers, and run a request for quotation, using only data it
is entitled to use: its own supplier records, feeds used under a recorded authorization, and quotes its
suppliers send back. The catalog does not depend on a right to redistribute a third party's catalog.

## Data kinds

| Kind | What it is | Requirement to register |
|---|---|---|
| `customer_records` | The tenant's own supplier master and price lists | who supplied the records |
| `authorized_feed` | Third-party data used under stated terms | who granted it, the document, the quoted words, the date; must permit `internal_sourcing`; must not have lapsed |
| `supplier_quote` | A supplier's reply to an RFQ | created by `record_quote`, tied to the quote document's digest |
| `synthetic` | Fixtures and demos | none; hidden from search unless requested, never ranked |

A source is visible to the tenant that owns it. `share()` to another tenant is refused unless the source's
authorization lists `redistribution`. A feed whose authorization has lapsed drops out of search.

## Item classes

- `verified`: a supplier quote, or a customer record with a source document and a validity date that has not passed.
- `unconfirmed`: a feed list price, a record with no validity date or document, or an expired price.
- `synthetic`.

`compare()` recommends `buy` only from a verified price matched on an identifier (GTIN or part number).
Otherwise it recommends `request_quote`. Offers below their minimum order quantity or outside the needed
lead time are listed with the reason and not ranked. Prices in different currencies are not ranked against
each other; no exchange rate is assumed. Money is an integer in minor units.

## Ingestion

`Catalog.ingest()` / `ingest_csv()` accept rows with: `supplier_ref`, `sku`, `title`, `currency`,
`unit_price` or `price_tiers` (`100:0.50;1000:0.41`), `moq`, `unit`, `pack_qty`, `lead_time_days`, `gtin`,
`mpn`, `brand`, `category`, `incoterm`, `country_of_origin`, `valid_from`, `valid_until`, `document_ref`,
`supplier_name`. A row that fails validation is rejected with its reason; the rest are kept. A newer row
for the same supplier and SKU supersedes the older one, which stays in the store.

`procurement/feeds.py` holds feed adapters. Open Food Facts (ODbL) supplies product identity by barcode and
no price.

## RFQ workflow

`draft` → `send` → `record_quote` → `compare` → `award`

- An RFQ goes only to suppliers in the tenant's own records that have a contact address there.
- Each supplier's message contains the requirement and nothing about other suppliers.
- `procurement.rfq.send` and `procurement.rfq.award` are approval-gated. Without a runtime-delivered
  approval they return a staged preview and do nothing.
- Delivery: `PROCUREMENT_SMTP_URL` (`smtp://host:port` or `smtps://user:password@host:port`) sends by SMTP;
  `PROCUREMENT_RFQ_OUTBOX` writes `.eml` files and transmits nothing. With neither set, `send` reports that
  no delivery is configured.
- A recorded quote needs the quote document and a validity date. Its prices become `supplier_quote`
  catalog data.
- `compare` ranks suppliers that quoted every line at a usable quantity in one currency, and lists the
  others with the reason. `award` produces a purchase-order draft; it is not written to an ERP.

## Supplier identity

`verify_supplier_identity()` checks a supplier's legal name against a `COMPANY_IDENTITY` provider (GLEIF).
The result is `verified` when exactly one register entry carries that legal name, otherwise `ambiguous`
(candidates kept), `no_match` or `unavailable`.

## Capabilities

`build_procurement_operator(catalog, book, tenant=...)` builds the operator for one tenant; the tenant is
not read from the input.

| Capability | Approval |
|---|---|
| `procurement.catalog.search` | no |
| `procurement.offers.compare` | no |
| `procurement.rfq.draft` | no |
| `procurement.rfq.send` | yes |
| `procurement.rfq.compare` | no |
| `procurement.rfq.award` | yes |

## Tests

`tests/test_procurement_catalog.py`, `tests/test_procurement_rfq.py`.
