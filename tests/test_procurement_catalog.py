"""Procurement catalog: what may be ingested, who may see it, and what a comparison is allowed to recommend."""
from __future__ import annotations

import pytest

from agentic_os.procurement.catalog import (
    AUTHORIZED_FEED, CUSTOMER_RECORDS, INTERNAL_SOURCING, REDISTRIBUTION, SYNTHETIC, UNCONFIRMED, VERIFIED, Catalog,
    CatalogError, CatalogSource, item_from_row, to_minor,
)

ON = "2026-11-23"
FEED = dict(authorized_by="Example Distribution Ltd", authorization_ref="https://example.test/partner-agreement-2026.pdf",
            authorization_quote="Licensee may use the Product Data for its internal purchasing.", authorized_at="2026-10-01")


def _row(**kw):
    base = {"supplier_ref": "S1", "supplier_name": "Northwind Fasteners", "sku": "NW-M8", "title": "Hex bolt M8x40 zinc",
            "currency": "USD", "unit_price": "0.42", "moq": "100", "lead_time_days": "7", "mpn": "HB-M8-40",
            "valid_until": "2027-01-31", "document_ref": "price-agreement-2026-Q4.pdf"}
    return {**base, **kw}


@pytest.fixture
def cat():
    c = Catalog()
    c.register_source(CatalogSource("acme-erp", "acme", CUSTOMER_RECORDS, "Acme ERP price list", authorized_by="buyer@acme.test"))
    return c


def test_money_is_exact_minor_units():
    assert to_minor("0.42", "usd") == 42 and to_minor("1200", "JPY") == 1200 and to_minor("1.250", "KWD") == 1250
    for bad in (("0.425", "USD"), ("-1", "USD"), ("1.5", "JPY"), ("abc", "USD"), ("1", "US")):
        with pytest.raises(CatalogError):
            to_minor(*bad)


# ── what may be registered ──
def test_a_feed_without_a_recorded_authorization_is_refused(cat):
    with pytest.raises(CatalogError, match="needs a recorded authorization"):
        cat.register_source(CatalogSource("f1", "acme", AUTHORIZED_FEED, "Some marketplace"))
    with pytest.raises(CatalogError, match="authorization_quote"):
        cat.register_source(CatalogSource("f1", "acme", AUTHORIZED_FEED, "Some marketplace",
                                          **{**FEED, "authorization_quote": " "}))


def test_a_feed_whose_authorization_lapsed_or_excludes_sourcing_is_refused(cat):
    with pytest.raises(CatalogError, match="lapsed"):
        cat.register_source(CatalogSource("f1", "acme", AUTHORIZED_FEED, "Feed", expires_at="2020-01-01", **FEED))
    with pytest.raises(CatalogError, match="does not permit internal sourcing"):
        cat.register_source(CatalogSource("f1", "acme", AUTHORIZED_FEED, "Feed", permitted_uses=(REDISTRIBUTION,), **FEED))


def test_another_tenant_cannot_take_over_a_source_id(cat):
    with pytest.raises(CatalogError, match="another tenant"):
        cat.register_source(CatalogSource("acme-erp", "globex", CUSTOMER_RECORDS, "x", authorized_by="x"))


# ── ingestion ──
def test_bad_rows_are_rejected_with_reasons_and_good_rows_kept(cat):
    res = cat.ingest("acme", "acme-erp", [_row(), _row(sku=""), _row(sku="B", unit_price="0.425"), _row(sku="C", gtin="12ab"),
                                           _row(sku="D", valid_until="31/01/2027"), _row(sku="E", moq="0")])
    assert (res["accepted"], res["rejected"]) == (1, 5)
    assert [r["row"] for r in res["rejects"]] == [2, 3, 4, 5, 6]
    assert "more decimals" in res["rejects"][1]["reason"] and "YYYY-MM-DD" in res["rejects"][3]["reason"]


def test_csv_with_an_unknown_column_is_refused_whole(cat):
    with pytest.raises(CatalogError, match="unknown column"):
        cat.ingest_csv("acme", "acme-erp", "supplier_ref,sku,title,margin\nS1,A,Bolt,0.3\n")
    res = cat.ingest_csv("acme", "acme-erp", "supplier_ref,sku,title,currency,price_tiers,moq\nS1,A,Bolt,EUR,100:0.50;1000:0.41,100\n")
    assert res["accepted"] == 1
    assert cat.search("acme", text="bolt")[0]["price_tiers"] == [[100.0, 50], [1000.0, 41]]


def test_a_tenant_cannot_ingest_into_a_source_it_does_not_own(cat):
    with pytest.raises(CatalogError, match="no such catalog source"):
        cat.ingest("globex", "acme-erp", [_row()])
    with pytest.raises(CatalogError, match="no such catalog source"):
        cat.ingest("acme", "missing", [_row()])


def test_a_newer_row_supersedes_and_the_old_one_is_kept(cat):
    cat.ingest("acme", "acme-erp", [_row()])
    cat.ingest("acme", "acme-erp", [_row(unit_price="0.40")])
    hits = cat.search("acme", text="hex bolt", on=ON)
    assert len(hits) == 1 and hits[0]["price_tiers"] == [[100.0, 40]]
    assert cat._db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2


# ── who may see what ──
def test_one_tenant_never_sees_anothers_records(cat):
    cat.ingest("acme", "acme-erp", [_row()])
    assert cat.search("globex", text="bolt") == [] and cat.search("globex", identifiers={"mpn": "HB-M8-40"}) == []
    assert cat.compare("globex", quantity=100, text="bolt")["recommendation"]["action"] == "no_offer"
    assert cat.suppliers("globex") == []


def test_sharing_needs_a_redistribution_right(cat):
    cat.ingest("acme", "acme-erp", [_row()])
    with pytest.raises(CatalogError, match="does not permit redistribution"):
        cat.share("acme", "acme-erp", "globex")
    with pytest.raises(CatalogError, match="no such catalog source"):
        cat.share("globex", "acme-erp", "globex")                                       # nor can the other tenant grant it to itself
    with pytest.raises(CatalogError, match="needs the document"):
        cat.register_source(CatalogSource("open", "acme", CUSTOMER_RECORDS, "x", authorized_by="x",
                                          permitted_uses=(INTERNAL_SOURCING, REDISTRIBUTION)))
    cat.register_source(CatalogSource("feed", "acme", AUTHORIZED_FEED, "Licensed feed",
                                      permitted_uses=(INTERNAL_SOURCING, REDISTRIBUTION), attribution="Data: Example Distribution", **FEED))
    cat.ingest("acme", "feed", [_row(sku="F1", valid_until="")])
    cat.share("acme", "feed", "globex")
    seen = cat.search("globex", text="bolt")
    assert [h["sku"] for h in seen] == ["F1"] and seen[0]["source"]["attribution"] == "Data: Example Distribution"


def test_a_feed_stops_being_searchable_when_its_authorization_lapses(cat):
    cat.register_source(CatalogSource("feed", "acme", AUTHORIZED_FEED, "Feed", expires_at="2026-12-31", **FEED))
    cat.ingest("acme", "feed", [_row(sku="F1")])
    assert [h["sku"] for h in cat.search("acme", text="bolt", on="2026-12-31")] == ["F1"]
    assert cat.search("acme", text="bolt", on="2027-01-01") == []


# ── verified, unconfirmed, synthetic ──
@pytest.mark.parametrize("change,cls,why", [
    ({}, VERIFIED, "customer price record"),
    ({"valid_until": "2026-01-31"}, UNCONFIRMED, "validity ended"),
    ({"valid_until": ""}, UNCONFIRMED, "no validity date"),
    ({"document_ref": ""}, UNCONFIRMED, "no agreement or quote document"),
    ({"unit_price": ""}, UNCONFIRMED, "no price"),
])
def test_a_customer_record_is_verified_only_with_a_document_and_a_live_validity(cat, change, cls, why):
    cat.ingest("acme", "acme-erp", [_row(**change)])
    hit = cat.search("acme", text="bolt", on=ON)[0]
    assert hit["class"] == cls and why in hit["class_reason"]


def test_a_feed_list_price_is_never_a_verified_offer(cat):
    cat.register_source(CatalogSource("feed", "acme", AUTHORIZED_FEED, "Feed", **FEED))
    cat.ingest("acme", "feed", [_row()])
    hit = cat.search("acme", text="bolt", on=ON)[0]
    assert hit["class"] == UNCONFIRMED and "list price" in hit["class_reason"]


def test_synthetic_rows_are_hidden_by_default_and_never_ranked(cat):
    cat.register_source(CatalogSource("demo", "acme", SYNTHETIC, "Demo fixture"))
    cat.ingest("acme", "demo", [_row(unit_price="0.01")])
    assert cat.search("acme", text="bolt") == []
    shown = cat.search("acme", text="bolt", include_synthetic=True)
    assert shown[0]["class"] == SYNTHETIC
    cmp_ = cat.compare("acme", quantity=100, text="bolt", include_synthetic=True)
    assert cmp_["offers"] == {} and cmp_["excluded"][0]["excluded_reason"] == "synthetic fixture"
    with pytest.raises(CatalogError, match="not redistributed"):
        cat.register_source(CatalogSource("demo2", "acme", SYNTHETIC, "x", permitted_uses=(REDISTRIBUTION,)))


# ── search and comparison ──
def test_identifier_search_is_like_for_like_across_gtin_forms(cat):
    cat.ingest("acme", "acme-erp", [_row(gtin="0012345678905", mpn=""), _row(sku="other", gtin="4006381333931", mpn="")])
    hits = cat.search("acme", identifiers={"upc": "012345678905"}, on=ON)
    assert [h["sku"] for h in hits] == ["NW-M8"] and hits[0]["matched_on"] == "identifier"
    assert [h["sku"] for h in cat.search("acme", identifiers={"mpn": "hb m8 40"}, on=ON)] == []
    cat.ingest("acme", "acme-erp", [_row(sku="P", mpn="HB-M8-40")])
    assert [h["sku"] for h in cat.search("acme", identifiers={"mpn": "hb m8 40"}, on=ON)] == ["P"]


def _two_suppliers(cat):
    cat.ingest("acme", "acme-erp", [
        _row(),                                                                             # S1 verified 0.42, MOQ 100, 7 days
        _row(supplier_ref="S2", supplier_name="Contoso Bolts", sku="CB-8", price_tiers="50:0.45;500:0.38", unit_price="",
             moq="50", lead_time_days="21"),                                              # S2 verified, cheaper from 500
        _row(supplier_ref="S3", supplier_name="Old Quote Co", sku="OQ-8", unit_price="0.20", valid_until="2026-01-01"),
    ])


def test_buy_is_recommended_only_from_a_verified_like_for_like_price(cat):
    _two_suppliers(cat)
    c = cat.compare("acme", quantity=100, identifiers={"mpn": "HB-M8-40"}, on=ON)
    assert c["recommendation"]["action"] == "request_quote" and "validity ended" in c["recommendation"]["reason"]
    assert c["recommendation"]["verified_alternative"] == {"supplier_ref": "S1", "sku": "NW-M8", "total_minor": 4200}
    cat.ingest("acme", "acme-erp", [_row(supplier_ref="S3", sku="OQ-8", unit_price="0.50")])       # the stale price is replaced
    c = cat.compare("acme", quantity=100, identifiers={"mpn": "HB-M8-40"}, on=ON)
    assert c["recommendation"] == {"action": "buy", "supplier_ref": "S1", "sku": "NW-M8",
                                   "reason": "lowest verified like-for-like price: customer price record valid until 2027-01-31"}


def test_quantity_picks_the_tier_and_moq_and_lead_time_exclude(cat):
    _two_suppliers(cat)
    cat.ingest("acme", "acme-erp", [_row(supplier_ref="S3", sku="OQ-8", unit_price="0.50")])
    big = cat.compare("acme", quantity=500, identifiers={"mpn": "HB-M8-40"}, on=ON)
    assert big["recommendation"]["supplier_ref"] == "S2" and big["offers"]["USD"][0]["total_minor"] == 19000
    small = cat.compare("acme", quantity=60, identifiers={"mpn": "HB-M8-40"}, on=ON)
    assert small["recommendation"]["supplier_ref"] == "S2"
    assert sorted(e["excluded_reason"] for e in small["excluded"]) == ["below the minimum order quantity of 100"] * 2
    urgent = cat.compare("acme", quantity=500, identifiers={"mpn": "HB-M8-40"}, need_by_days=10, on=ON)
    assert urgent["recommendation"]["supplier_ref"] == "S1"
    assert any("lead time 21 days exceeds 10" == e["excluded_reason"] for e in urgent["excluded"])


def test_a_text_match_is_a_candidate_not_a_like_for_like_buy(cat):
    cat.ingest("acme", "acme-erp", [_row(mpn="")])
    c = cat.compare("acme", quantity=100, text="hex bolt m8", on=ON)
    assert c["recommendation"]["action"] == "request_quote" and "matched on text" in c["recommendation"]["reason"]


def test_prices_in_different_currencies_are_not_ranked_against_each_other(cat):
    cat.ingest("acme", "acme-erp", [_row(), _row(supplier_ref="S2", sku="EU", currency="EUR", unit_price="0.30")])
    c = cat.compare("acme", quantity=100, identifiers={"mpn": "HB-M8-40"}, on=ON)
    assert sorted(c["offers"]) == ["EUR", "USD"] and c["recommendation"]["action"] == "review"
    assert "no exchange rate is assumed" in c["recommendation"]["reason"]


def test_item_from_row_rejects_a_tier_above_the_moq():
    with pytest.raises(CatalogError, match="lowest price tier"):
        item_from_row(_row(unit_price="", price_tiers="500:0.38", moq="100"))
