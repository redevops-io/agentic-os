"""RFQ workflow: who may be asked, what leaves, what is gated, how quotes are compared and awarded."""
from __future__ import annotations

import pytest

from agentic_os.procurement.catalog import CUSTOMER_RECORDS, VERIFIED, Catalog, CatalogError, CatalogSource
from agentic_os.procurement.feeds import OFF_ATTRIBUTION, open_food_facts_rows, open_food_facts_source
from agentic_os.procurement.rfq import (
    OutboxSender, RfqBook, SmtpSender, build_procurement_operator, sender_from_env, verify_supplier_identity,
)

LINES = [{"description": "Hex bolt M8x40 zinc", "quantity": 500, "identifiers": {"mpn": "HB-M8-40"}},
         {"description": "Washer M8", "quantity": 500}]
APPROVED = {"_approval": {"approved": True}}


@pytest.fixture
def env(tmp_path):
    cat = Catalog()
    cat.register_source(CatalogSource("acme-erp", "acme", CUSTOMER_RECORDS, "Acme ERP", authorized_by="buyer@acme.test"))
    cat.upsert_supplier("acme", "S1", name="Northwind Fasteners", email="sales@northwind.test")
    cat.upsert_supplier("acme", "S2", name="Contoso Bolts", email="quotes@contoso.test")
    cat.upsert_supplier("acme", "S3", name="No Contact Ltd")
    cat.upsert_supplier("globex", "G1", name="Globex Only", email="x@globex-supplier.test")
    book = RfqBook(cat, from_address="buyer@acme.test", sender=OutboxSender(str(tmp_path / "outbox")))
    return cat, book, tmp_path


def _draft(book, refs=("S1", "S2")):
    return book.draft("acme", lines=LINES, supplier_refs=list(refs), respond_by="2099-01-15", need_by="2099-02-01", incoterm="dap")


def _quote(book, rfq_id, ref, p1, p2, currency="USD", **kw):
    return book.record_quote("acme", rfq_id, ref, document=f"quote from {ref}".encode(), document_name=f"{ref}-quote.pdf",
                             received_from=f"sales@{ref}.test", valid_until=kw.pop("valid_until", "2099-03-01"),
                             lines=[{"line": 1, "unit_price": p1, "currency": currency, "moq": kw.get("moq", 100), "lead_time_days": 7},
                                    {"line": 2, "unit_price": p2, "currency": currency, "moq": 100, "lead_time_days": 9}][:kw.get("n", 2)])


# ── who may be asked ──
def test_only_the_tenants_own_suppliers_with_a_recorded_contact_can_be_asked(env):
    _, book, _ = env
    with pytest.raises(CatalogError, match="not in this tenant's supplier records"):
        _draft(book, ["S1", "G1"])                                                          # another tenant's supplier
    with pytest.raises(CatalogError, match="no contact address"):
        _draft(book, ["S3"])
    with pytest.raises(CatalogError, match="at least one supplier"):
        _draft(book, [])
    with pytest.raises(CatalogError, match="has not passed"):
        book.draft("acme", lines=LINES, supplier_refs=["S1"], respond_by="2020-01-01")
    with pytest.raises(CatalogError, match="positive quantity"):
        book.draft("acme", lines=[{"description": "x", "quantity": 0}], supplier_refs=["S1"], respond_by="2099-01-01")


def test_a_draft_sends_nothing_and_is_invisible_to_other_tenants(env):
    _, book, tmp = env
    rfq = _draft(book)
    assert rfq["state"] == "draft" and not (tmp / "outbox").exists()
    with pytest.raises(CatalogError, match="no such RFQ"):
        book.get("globex", rfq["rfq_id"])


def test_each_supplier_gets_the_requirement_and_nothing_about_the_others(env):
    _, book, _ = env
    rfq = _draft(book)
    a, b = (book.message_for(rfq, r) for r in rfq["recipients"])
    body = a.get_content()
    assert a["To"] == "sales@northwind.test" and b["To"] == "quotes@contoso.test"
    assert "Hex bolt M8x40 zinc (MPN HB-M8-40) - 500 each" in body and "reply by 2099-01-15" in body and "DAP" in body
    assert "Contoso" not in body and "contoso" not in str(a) and "Northwind" not in b.get_content()


# ── the gate ──
def test_send_and_award_do_nothing_without_approval(env):
    cat, book, tmp = env
    op = build_procurement_operator(cat, book, tenant="acme")
    rfq = op.invoke("procurement.rfq.draft", {"lines": LINES, "supplier_refs": ["S1", "S2"], "respond_by": "2099-01-15"})["rfq"]
    staged = op.invoke("procurement.rfq.send", {"rfq_id": rfq["rfq_id"]})
    assert staged["status"] == "pending_approval" and staged["executed"] is False and staged["rfq_sent"] is False
    assert not (tmp / "outbox").exists() and book.get("acme", rfq["rfq_id"])["state"] == "draft"
    sent = op.invoke("procurement.rfq.send", {"rfq_id": rfq["rfq_id"], **APPROVED})
    assert sent["status"] == "done" and sent["state"] == "sent" and sent["transmitted"] is False        # the outbox transmits nothing
    assert len(list((tmp / "outbox").glob("*.eml"))) == 2
    _quote(book, rfq["rfq_id"], "S1", "0.42", "0.05")
    assert op.invoke("procurement.rfq.award", {"rfq_id": rfq["rfq_id"], "supplier_ref": "S1"})["status"] == "pending_approval"
    assert book.get("acme", rfq["rfq_id"])["state"] == "quoted"
    done = op.invoke("procurement.rfq.award", {"rfq_id": rfq["rfq_id"], "supplier_ref": "S1", **APPROVED})
    assert done["rfq_awarded"] is True and done["purchase_order_draft"]["total_minor"] == 23500


def test_the_operator_is_bound_to_its_tenant_whatever_the_input_says(env):
    cat, book, _ = env
    cat.ingest("acme", "acme-erp", [{"supplier_ref": "S1", "sku": "A", "title": "Hex bolt", "currency": "USD", "unit_price": "1"}])
    other = build_procurement_operator(cat, book, tenant="globex")
    assert other.invoke("procurement.catalog.search", {"text": "bolt", "tenant": "acme"})["items"] == []
    rfq = _draft(book)
    res = other.invoke("procurement.rfq.send", {"rfq_id": rfq["rfq_id"], "tenant": "acme", **APPROVED})
    assert res["status"] == "refused" and "no such RFQ" in res["error"]
    specs = {c.name: c for c in other.manifest.capabilities}
    assert {n for n, s in specs.items() if s.approval_required} == {"procurement.rfq.send", "procurement.rfq.award"}
    assert all(s.irreversible for s in specs.values() if s.approval_required)


def test_send_is_refused_when_the_contact_changed_or_no_delivery_exists(env):
    cat, book, _ = env
    rfq = _draft(book)
    cat.upsert_supplier("acme", "S2", name="Contoso Bolts", email="someone-else@contoso.test")
    res = book.send("acme", rfq["rfq_id"])
    assert [("error" in r) for r in res["results"]] == [False, True] and "contact changed" in res["results"][1]["error"]
    with pytest.raises(CatalogError, match="already sent"):
        book.send("acme", rfq["rfq_id"])
    bare = RfqBook(cat)
    with pytest.raises(CatalogError, match="no delivery is configured"):
        bare.send("acme", bare.draft("acme", lines=LINES, supplier_refs=["S1"], respond_by="2099-01-01")["rfq_id"])


def test_sender_selection_from_env(monkeypatch, tmp_path):
    for k in ("PROCUREMENT_SMTP_URL", "PROCUREMENT_RFQ_OUTBOX"):
        monkeypatch.delenv(k, raising=False)
    assert sender_from_env() is None
    monkeypatch.setenv("PROCUREMENT_RFQ_OUTBOX", str(tmp_path))
    assert isinstance(sender_from_env(), OutboxSender)
    monkeypatch.setenv("PROCUREMENT_SMTP_URL", "smtp://127.0.0.1:1025")
    assert isinstance(sender_from_env(), SmtpSender)
    with pytest.raises(CatalogError):
        SmtpSender("http://example.test")


# ── quotes ──
def _sent(book):
    rfq = _draft(book)
    book.send("acme", rfq["rfq_id"])
    return rfq["rfq_id"]


def test_a_quote_needs_a_sent_rfq_an_asked_supplier_a_document_and_a_live_validity(env):
    cat, book, _ = env
    draft = _draft(book)
    with pytest.raises(CatalogError, match="has been sent"):
        _quote(book, draft["rfq_id"], "S1", "0.42", "0.05")
    rid = _sent(book)
    cat.upsert_supplier("acme", "S9", name="Uninvited", email="u@uninvited.test")
    with pytest.raises(CatalogError, match="was not asked"):
        _quote(book, rid, "S9", "0.1", "0.1")
    with pytest.raises(CatalogError, match="validity date"):
        _quote(book, rid, "S1", "0.42", "0.05", valid_until="2020-01-01")
    with pytest.raises(CatalogError, match="needs the document"):
        book.record_quote("acme", rid, "S1", lines=[], document=b"", document_name="", received_from="x", valid_until="2099-01-01")
    with pytest.raises(CatalogError, match="more decimals"):
        _quote(book, rid, "S1", "0.425", "0.05")


def test_a_recorded_quote_becomes_verified_catalog_data_tied_to_its_document(env):
    cat, book, _ = env
    rid = _sent(book)
    res = _quote(book, rid, "S1", "0.42", "0.05")
    assert res["document_ref"].startswith("sha256:") and res["document_ref"].endswith(":S1-quote.pdf")
    hit = cat.search("acme", identifiers={"mpn": "HB-M8-40"})[0]
    assert hit["class"] == VERIFIED and "supplier quote" in hit["class_reason"] and hit["document_ref"] == res["document_ref"]
    assert cat.compare("acme", quantity=500, identifiers={"mpn": "HB-M8-40"})["recommendation"]["action"] == "buy"
    assert cat.search("globex", identifiers={"mpn": "HB-M8-40"}) == []


def test_comparison_ranks_complete_quotes_and_explains_the_rest(env):
    cat, book, _ = env
    cat.upsert_supplier("acme", "S4", name="Partial Parts", email="p@partial.test")
    cat.upsert_supplier("acme", "S5", name="Big Batch", email="b@bigbatch.test")
    rid = book.draft("acme", lines=LINES, supplier_refs=["S1", "S2", "S4", "S5"], respond_by="2099-01-15")["rfq_id"]
    book.send("acme", rid)
    _quote(book, rid, "S1", "0.42", "0.05")            # 235.00
    _quote(book, rid, "S4", "0.10", "0.01", n=1)       # one line only
    _quote(book, rid, "S5", "0.10", "0.01", moq=1000)  # MOQ above the need
    c = book.compare("acme", rid)
    assert [r["supplier_ref"] for r in c["ranked"]] == ["S1"] and c["ranked"][0]["total_minor"] == 23500
    assert {s["supplier_ref"]: s["reason"] for s in c["set_aside"]} == {
        "S4": "does not quote every line: missing 2", "S5": "a line is below the quoted minimum order quantity"}
    assert c["no_reply"] == ["S2"] and c["recommendation"]["supplier_ref"] == "S1"
    _quote(book, rid, "S2", "0.40", "0.05")            # 225.00
    assert book.compare("acme", rid)["recommendation"]["supplier_ref"] == "S2"


def test_quotes_in_different_currencies_are_not_ranked_against_each_other(env):
    _, book, _ = env
    rid = _sent(book)
    _quote(book, rid, "S1", "0.42", "0.05")
    _quote(book, rid, "S2", "0.30", "0.04", currency="EUR")
    c = book.compare("acme", rid)
    assert c["recommendation"]["action"] == "review" and "no exchange rate" in c["recommendation"]["reason"]


def test_award_produces_a_purchase_order_draft_once_and_flags_a_non_lowest_choice(env):
    _, book, _ = env
    rid = _sent(book)
    _quote(book, rid, "S1", "0.42", "0.05")
    _quote(book, rid, "S2", "0.40", "0.05")
    with pytest.raises(CatalogError, match="no complete, current quote"):
        book.award("acme", rid, "S3")
    po = book.award("acme", rid, "S1")["purchase_order_draft"]
    assert po["status"] == "draft" and po["supplier_ref"] == "S1" and po["not_lowest"] is True
    assert po["quote_document_ref"].startswith("sha256:") and [ln["unit_price_minor"] for ln in po["lines"]] == [42, 5]
    with pytest.raises(CatalogError, match="no earlier award"):
        book.award("acme", rid, "S2")
    assert [e["event"] for e in book.events("acme", rid)] == ["drafted", "sent", "quote_recorded", "quote_recorded", "awarded"]


# ── supplier identity and the authorized feed ──
def test_supplier_identity_is_verified_only_on_one_exact_register_entry(env):
    from agentic_os.intelligence.adapters.gleif import GleifProvider
    cat, _, _ = env
    rec = lambda lei, name: {"attributes": {"lei": lei, "entity": {"legalName": {"name": name}, "status": "ACTIVE", "jurisdiction": "DE"},  # noqa: E731
                                            "registration": {"status": "ISSUED"}}}
    one = GleifProvider(fetch=lambda u, h: (200, {"data": [rec("L1", "NORTHWIND FASTENERS"), rec("L2", "Northwind Fasteners Holding")]}))
    got = verify_supplier_identity(cat, "acme", "S1", one)
    assert got["status"] == "verified" and got["record"]["lei"] == "L1"
    assert cat.supplier("acme", "S1")["external_ids"]["lei"] == "L1" and cat.supplier("acme", "S1")["email"] == "sales@northwind.test"
    two = GleifProvider(fetch=lambda u, h: (200, {"data": [rec("L1", "Contoso Bolts"), rec("L2", "CONTOSO BOLTS")]}))
    amb = verify_supplier_identity(cat, "acme", "S2", two)
    assert amb["status"] == "ambiguous" and len(amb["candidates"]) == 2 and "lei" not in cat.supplier("acme", "S2")["external_ids"]
    none = GleifProvider(fetch=lambda u, h: (200, {"data": []}))
    assert verify_supplier_identity(cat, "acme", "S3", none)["status"] == "no_match"
    down = GleifProvider(fetch=lambda u, h: (503, {}))
    assert verify_supplier_identity(cat, "acme", "S3", down)["status"] == "unavailable"


def test_open_food_facts_rows_carry_identity_no_price_and_the_licence(env):
    cat, _, _ = env
    def fetch(url, headers):
        if "3017620422003" in url:
            return 200, {"status": 1, "product": {"product_name": "Nutella", "brands": "Ferrero, Nutella", "quantity": "400 g",
                                                  "categories_tags": ["en:spreads", "en:hazelnut-spreads"]}}
        return 404, {"status": 0}
    rows, misses = open_food_facts_rows(["3017620422003", "0000000000000", "12"], fetch=fetch)
    assert [r["gtin"] for r in rows] == ["3017620422003"] and rows[0]["title"] == "Nutella 400 g" and "unit_price" not in rows[0]
    assert [m["reason"] for m in misses] == ["not in the database (http 404)", "not an 8 to 14 digit barcode"]
    src = cat.register_source(open_food_facts_source("acme", recorded_by="buyer@acme.test"))
    assert src.attribution == OFF_ATTRIBUTION
    cat.ingest("acme", src.source_id, rows)
    c = cat.compare("acme", quantity=24, identifiers={"ean": "3017620422003"})
    assert c["offers"] == {} and c["excluded"][0]["excluded_reason"] == "no price on record"
    assert c["excluded"][0]["source"]["attribution"] == OFF_ATTRIBUTION
