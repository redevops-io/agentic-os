"""Economic events over ERPNext's documents: what each document becomes, what is a gap rather than a zero, and the five answers.

The fixtures have the shape of a real ERPNext 16 answer (field names and the way the application leaves out a field it will not give).
The same code was also run against a real, isolated ERPNext with a seeded quarter; those figures are in the acceptance evidence, not here.
"""
from decimal import Decimal

import pytest

from agentic_os.economics import answers, reconcile, reliability, verify
from agentic_os.economics.contracts import Basis, Kind
from agentic_os.economics.erpnext_events import extract
from agentic_os.integrations.erpnext import ErpnextClient, ErpnextReadError


class Erp:
    """A client with a fixed answer per doctype. ``fail`` names doctypes that raise; ``drop`` names (doctype, field) the application leaves out."""

    def __init__(self, rows, fail=(), drop=()):
        self.rows, self.fail, self.drop = rows, set(fail), set(drop)

    def read_list(self, doctype, fields, filters=None, limit=0):
        if doctype in self.fail:
            raise ErpnextReadError(f"{doctype}: the application answered HTTP 500")
        rows = self.rows.get(doctype, [])
        keep = [f for f in fields if (doctype, f) not in self.drop]
        for f in fields:
            if f.startswith("custom_") and not any(f in r for r in rows):
                raise ErpnextReadError(f"{doctype}: no such column {f}")
        return [{k: r[k] for k in keep if k in r} for r in rows]


def si(name, customer, day, net, **kw):
    return {"name": name, "customer": customer, "posting_date": day, "docstatus": 1, "is_return": 0, "return_against": None, "amended_from": None,
            "net_total": net, "currency": "USD", **kw}


def item(parent, qty, lst, rate, cost):
    return {"parent": parent, "item_code": "W", "qty": qty, "price_list_rate": lst, "rate": rate, "net_amount": qty * rate, "incoming_rate": cost}


def base_rows():
    return {
        "Sales Invoice": [si("S1", "Acme", "2026-07-05", 2000), si("S2", "Acme", "2026-08-12", 1800), si("S3", "Birch", "2026-07-10", 2250),
                          si("S4", "Birch", "2026-09-02", 1440),
                          si("S5", "Cedar", "2026-08-15", 1000, docstatus=2), si("S6", "Dune", "2026-07-30", 3000, docstatus=2),
                          si("S6-1", "Dune", "2026-07-30", 3000, amended_from="S6"),
                          si("CN1", "Birch", "2026-09-20", -500, is_return=1, return_against="S3")],
        "Sales Invoice Item": [item("S1", 20, 100, 100, 40), item("S2", 10, 200, 180, 120), item("S3", 30, 100, 75, 40), item("S4", 8, 200, 200, 120),
                               item("S6-1", 15, 200, 200, 120), item("CN1", -5, 100, 100, 40)],
        "Comment": [{"reference_name": "S2", "content": "aos-approval id=APR-1 approver=usr-cfo action=apply_discount"},
                    {"reference_name": "S4", "content": "just a remark"}],
        "Purchase Invoice": [
            {"name": "P1", "supplier": "Northwind", "posting_date": "2026-08-05", "docstatus": 1, "is_return": 0, "return_against": None, "net_total": 4000, "currency": "USD"},
            {"name": "PD1", "supplier": "Northwind", "posting_date": "2026-09-20", "docstatus": 1, "is_return": 1, "return_against": "P1", "net_total": -400, "currency": "USD"},
            {"name": "P2", "supplier": "Quartz", "posting_date": "2026-06-25", "docstatus": 1, "is_return": 0, "return_against": None, "net_total": 3500, "currency": "USD"}],
        "Timesheet": [{"name": "T1", "docstatus": 1, "start_date": "2026-07-20"}, {"name": "T2", "docstatus": 1, "start_date": "2026-09-05"},
                      {"name": "T3", "docstatus": 1, "start_date": "2026-09-18"}, {"name": "T4", "docstatus": 0, "start_date": "2026-09-18"}],
        "Project": [{"name": "PR-A", "customer": "Acme"}, {"name": "PR-B", "customer": "Birch"}, {"name": "PR-INT", "customer": None}],
        "Timesheet Detail": [
            {"parent": "T1", "activity_type": "Support", "from_time": "2026-07-20 08:00:00", "hours": 2, "base_costing_amount": 100, "project": "PR-A",
             "custom_supplier": None, "custom_reference": None},
            {"parent": "T2", "activity_type": "Rework", "from_time": "2026-09-05 08:00:00", "hours": 26, "base_costing_amount": 1300, "project": "PR-B",
             "custom_supplier": None, "custom_reference": None},
            {"parent": "T3", "activity_type": "Rework", "from_time": "2026-09-18 08:00:00", "hours": 40, "base_costing_amount": 2000, "project": "PR-INT",
             "custom_supplier": "Quartz", "custom_reference": None},
            {"parent": "T4", "activity_type": "Support", "from_time": "2026-09-18 08:00:00", "hours": 9, "base_costing_amount": 450, "project": "PR-A",
             "custom_supplier": None, "custom_reference": None}],
    }


def ev(rows=None, **kw):
    return extract(Erp(rows or base_rows(), **kw))


def by_id(ex, i):
    return next(e for e in ex.events if e.id == i)


# ── what each document becomes ─────────────────────────────────────────────────────────────────────────────
def test_a_discounted_invoice_is_revenue_at_list_price_and_a_separate_discount_that_ties_out_to_the_invoice_total():
    ex = ev()
    sale, disc = by_id(ex, "S2:sale"), by_id(ex, "S2:discount")
    assert (sale.kind, sale.amount, disc.kind, disc.amount) == (Kind.SALE, Decimal("2000.00"), Kind.DISCOUNT, Decimal("-200.00"))
    assert sale.amount + disc.amount == Decimal("1800.00")                              # the invoice's own net total
    assert not any(e.id == "S1:discount" for e in ex.events)                            # no discount, no event


def test_a_discount_carries_the_approval_claim_written_in_a_comment_and_that_claim_is_never_verified():
    ex = ev()
    assert (by_id(ex, "S2:discount").approval.id, by_id(ex, "S2:discount").approval.approver) == ("APR-1", "usr-cfo")
    assert by_id(ex, "S3:discount").approval is None and by_id(ex, "S4:discount").approval is None
    a = by_id(ex, "S2:discount").approval
    assert a.verified is False and "unverified" in a.source and by_id(ex, "S2:discount").to_json()["approval"]["verified"] is False


def test_a_document_discount_is_found_even_when_no_item_carries_one():
    assert by_id(ev(), "S4:discount").amount == Decimal("-160.00")                      # list 1600 (8 x 200), invoiced 1440


def test_a_credit_note_is_negative_revenue_and_gives_cost_of_goods_back():
    ex = ev()
    assert (by_id(ex, "CN1:sale").kind, by_id(ex, "CN1:sale").amount) == (Kind.RETURN, Decimal("-500.00"))
    assert by_id(ex, "CN1:sale").supersedes == "erpnext:Sales Invoice/S3"
    assert by_id(ex, "CN1:cogs").amount == Decimal("200.00")


def test_a_cancelled_invoice_is_forgone_revenue_unless_an_amended_copy_replaced_it():
    ex = ev()
    gone, again = by_id(ex, "S5:cancellation"), by_id(ex, "S6:cancellation")
    assert (gone.basis, gone.amount) == (Basis.ESTIMATED, Decimal("-1000.00"))
    assert (again.amount, again.note) == (Decimal("0.00"), "replaced by S6-1")
    assert not any(e.id.startswith("S5:sale") for e in ex.events)                       # a cancelled invoice has no posted sale


def test_supplier_bills_and_debit_notes_and_time_logs_become_events_with_the_right_basis():
    ex = ev()
    assert (by_id(ex, "P1:supplier").amount, by_id(ex, "P1:supplier").basis) == (Decimal("-4000.00"), Basis.POSTED)
    assert by_id(ex, "PD1:supplier").amount == Decimal("400.00") and by_id(ex, "PD1:supplier").kind == Kind.SUPPLIER_RETURN
    t = by_id(ex, "T2:Rework:2026-09-05")
    assert (t.kind, t.basis, t.amount, t.subjects["customer"]) == (Kind.REWORK_COST, Basis.ESTIMATED, Decimal("-1300.00"), "Birch")
    assert not any(e.id.startswith("T4") for e in ex.events)                            # an unsubmitted timesheet is not cost yet
    assert by_id(ex, "T3:Rework:2026-09-18").subjects["supplier"] == "Quartz"


def test_a_customer_the_resolver_does_not_know_stays_unresolved_and_is_never_merged_with_another():
    ex = extract(Erp(base_rows()), customer_of=lambda n: {"Acme": "ent-1"}.get(n))
    assert by_id(ex, "S1:sale").subjects["customer"] == "ent-1" and by_id(ex, "S3:sale").subjects["customer"] == "unresolved:Birch"
    assert by_id(ex, "S3:sale").subjects["customer_source"] == "Birch"


# ── a gap is never a zero ──────────────────────────────────────────────────────────────────────────────────
def test_an_unreadable_doctype_is_a_recorded_gap_and_its_events_are_missing_not_zero():
    ex = ev(fail=["Purchase Invoice"])
    assert any(g["source"] == "erpnext:Purchase Invoice" for g in ex.gaps)
    assert answers.supplier_cost(ex.events)[0]["supplier"] == "Quartz"                  # only the rework time is known; no purchase is invented
    assert not any(e.kind in (Kind.SUPPLIER_COST, Kind.SUPPLIER_RETURN) for e in ex.events)


def test_a_field_the_application_leaves_out_without_an_error_is_a_gap():
    ex = ev(drop=[("Timesheet Detail", "base_costing_amount")])
    assert any("base_costing_amount" in g["what"] for g in ex.gaps)


def test_custom_fields_the_site_does_not_have_cost_supplier_attribution_not_the_whole_read_and_do_not_make_it_incomplete():
    rows = base_rows()
    for r in rows["Timesheet Detail"]:
        r.pop("custom_supplier"), r.pop("custom_reference")
    ex = ev(rows)
    assert any("custom_supplier" in g["what"] for g in ex.notes) and not ex.gaps          # said, but the read is complete without an optional extension
    assert by_id(ex, "T2:Rework:2026-09-05").amount == Decimal("-1300.00") and "supplier" not in by_id(ex, "T3:Rework:2026-09-18").subjects


def test_an_activity_type_that_is_not_mapped_is_reported_not_guessed():
    rows = base_rows()
    rows["Timesheet Detail"].append({"parent": "T1", "activity_type": "Planning", "from_time": "2026-07-21 08:00:00", "hours": 1, "base_costing_amount": 50,
                                     "project": "PR-A", "custom_supplier": None, "custom_reference": None})
    ex = ev(rows)
    assert any("Planning" in g["what"] for g in ex.gaps) and not any("Planning" in e.id for e in ex.events)


def test_an_invoice_whose_items_could_not_be_read_is_not_given_a_made_up_list_price():
    ex = ev(fail=["Sales Invoice Item"])
    assert any(g["source"] == "erpnext:Sales Invoice Item" for g in ex.gaps)
    assert not any(e.kind == Kind.DISCOUNT for e in ex.events) and by_id(ex, "S1:sale").amount == Decimal("2000.00")


# ── the five answers ───────────────────────────────────────────────────────────────────────────────────
def test_customer_profit_splits_posted_from_estimated_and_flags_the_unprofitable():
    rows = answers.customer_profitability(ev().events, since="2026-07-01", until="2026-09-30")
    acme = next(r for r in rows if r["customer"] == "Acme")
    assert (acme["net_revenue"], acme["cost_of_goods"], acme["support"], acme["profit"]) == ("3800.00", "-2000.00", "-100.00", "1700.00")
    assert (acme["profit_posted_part"], acme["profit_estimated_part"]) == ("1800.00", "-100.00")
    birch = next(r for r in rows if r["customer"] == "Birch")
    assert (birch["net_revenue"], birch["profit"]) == ("3190.00", "-70.00") and birch["unprofitable"] and birch["rework"] == "-1300.00"
    assert [r["customer"] for r in rows][0] == "Birch"                                  # worst first


def test_forgone_revenue_of_a_cancellation_is_shown_but_is_not_taken_off_profit():
    rows = answers.customer_profitability(ev().events)
    cedar = next(r for r in rows if r["customer"] == "Cedar")
    assert cedar["cancellation_revenue_forgone"] == "-1000.00" and cedar["profit"] == "0.00"


def test_cancellation_cost_is_estimated_and_its_posted_effect_is_nil():
    rows = base_rows()
    rows["Timesheet"].append({"name": "T5", "docstatus": 1, "start_date": "2026-09-03"})
    rows["Timesheet Detail"].append({"parent": "T5", "activity_type": "Cancellation Handling", "from_time": "2026-09-03 08:00:00", "hours": 3,
                                     "base_costing_amount": 150, "project": "PR-B", "custom_supplier": None, "custom_reference": "S5"})
    out = answers.cancellation_cost(ev(rows).events, since="2026-07-01", until="2026-09-30")
    assert (out["revenue_forgone_estimated"], out["handling_cost_estimated"], out["total_estimated"], out["posted_effect"]) == ("1000.00", "150.00", "1150.00", "0.00")
    assert (out["invoices_cancelled"], out["replaced"]) == (2, 1)


def test_discount_loss_names_the_claimed_approver_as_unverified_and_separates_the_discounts_with_no_claim():
    out = answers.discount_loss(ev().events, since="2026-07-01", until="2026-09-30")
    assert (out["total_posted"], out["with_approval_claim"], out["without_approval_claim"]) == ("1110.00", "200.00", "910.00")
    assert out["by_claimed_approver"] == {"usr-cfo": "200.00"} and "unverified" in out["approval_evidence"]


def test_supplier_cost_adds_attributed_rework_to_purchases_and_that_can_change_the_ranking():
    out = answers.supplier_cost(ev().events)
    assert [(r["supplier"], r["net_purchases_posted"], r["rework_estimated"], r["total"]) for r in out] == [
        ("Quartz", "3500.00", "2000.00", "5500.00"), ("Northwind", "3600.00", "0.00", "3600.00")]
    assert sorted(out, key=lambda r: -Decimal(r["net_purchases_posted"]))[0]["supplier"] == "Northwind"       # by purchases alone the order is the other way


# ── Q5: discrepancy → trace → proposal (not applied) ──────────────────────────────────────────────────────
def recon_rows(bank):
    return {"Payment Entry": [{"name": "PAY-1", "docstatus": 1, "party": "Cedar", "paid_amount": 3800, "reference_no": "WIRE-C1", "payment_type": "Receive"}],
            "Payment Entry Reference": [{"parent": "PAY-1", "reference_doctype": "Sales Invoice", "reference_name": "S5"}],
            "Bank Transaction": [{"name": "BT-1", "deposit": bank, "withdrawal": 0, "reference_number": "WIRE-C1", "docstatus": 1}]}


def test_a_payment_the_bank_shows_for_another_amount_is_traced_to_its_invoice_and_customer_and_proposed_never_applied():
    found = reconcile.find(Erp(recon_rows(3380)))
    d = found.events[0]
    assert (d.posted, d.bank, d.difference, d.customer, list(d.invoices)) == (Decimal("3800.00"), Decimal("3380.00"), Decimal("420.00"), "Cedar", ["S5"])
    p = reconcile.propose(d).to_json()
    assert p["applied"] is False and "PAY-1" in p["action"] and "approval" in p["requires"]


def test_a_matching_payment_is_not_a_discrepancy_and_an_unreadable_bank_is_not_a_clean_result():
    assert reconcile.find(Erp(recon_rows(3800))).events == []
    f = reconcile.find(Erp(recon_rows(3800), fail=["Bank Transaction"]))
    assert f.events == [] and any("no statement" in g["effect"] for g in f.gaps)


def test_a_payment_cancelled_and_entered_again_is_a_correction_and_it_says_whether_the_bank_now_agrees():
    rows = recon_rows(3380)
    rows["Payment Entry"] = [{"name": "PAY-1", "docstatus": 2, "party": "Cedar", "paid_amount": 3800, "reference_no": "WIRE-C1", "payment_type": "Receive"},
                             {"name": "PAY-2", "docstatus": 1, "party": "Cedar", "paid_amount": 3380, "reference_no": "WIRE-C1", "payment_type": "Receive"}]
    rows["Payment Entry Reference"] = [{"parent": "PAY-2", "reference_doctype": "Sales Invoice", "reference_name": "S5"}]
    k = reconcile.corrections(Erp(rows)).events[0]
    assert (k.cancelled_payment, k.cancelled_amount, k.replacement_payment, k.replacement_amount, k.reconciled, list(k.invoices)) == (
        "PAY-1", Decimal("3800.00"), "PAY-2", Decimal("3380.00"), True, ["S5"])
    assert reconcile.find(Erp(rows)).events == []                                       # and no discrepancy is open any more
    rows["Bank Transaction"][0]["deposit"] = 3000
    assert reconcile.corrections(Erp(rows)).events[0].reconciled is False


# ── the independent ledger check ───────────────────────────────────────────────────────────────────────
def gl_rows():
    acc = [{"name": "Sales - X", "root_type": "Income", "account_type": "Income Account"}, {"name": "COGS - X", "root_type": "Expense", "account_type": "Cost of Goods Sold"},
           {"name": "Creditors - X", "root_type": "Liability", "account_type": "Payable"}, {"name": "Debtors - X", "root_type": "Asset", "account_type": "Receivable"}]
    gl = [{"voucher_type": "Sales Invoice", "voucher_no": "S2", "account": "Sales - X", "debit": 0, "credit": 1800, "is_cancelled": 0},
          {"voucher_type": "Sales Invoice", "voucher_no": "S2", "account": "COGS - X", "debit": 1200, "credit": 0, "is_cancelled": 0},
          {"voucher_type": "Purchase Invoice", "voucher_no": "P1", "account": "Creditors - X", "debit": 0, "credit": 4000, "is_cancelled": 0}]
    return {"Account": acc, "GL Entry": gl, "Timesheet": [{"name": "T1", "docstatus": 1, "total_costing_amount": 100}]}


def test_the_ledger_agrees_with_the_events_and_a_changed_event_is_caught():
    rows = {**base_rows(), **gl_rows()}
    events = [e for e in extract(Erp(rows)).events if e.id.split(":")[0] in ("S2", "P1", "T1")]
    ok = verify.ledger_check(Erp(rows), events)
    assert ok["mismatched"] == [] and ok["matched"] == 3
    from agentic_os.economics.contracts import EconomicEvent
    tampered = [EconomicEvent(**{**e.__dict__, "amount": e.amount + Decimal("1")}) if e.id == "S2:discount" else e for e in events]
    bad = verify.ledger_check(Erp(rows), tampered)
    assert bad["mismatched"][0]["voucher"] == "S2" and "revenue" in bad["mismatched"][0]["differences"][0]


def test_a_voucher_with_taxes_or_no_ledger_lines_is_unverifiable_never_matching():
    rows = {**base_rows(), **gl_rows()}
    rows["GL Entry"] = [g for g in rows["GL Entry"] if g["voucher_no"] != "P1"]
    rows["GL Entry"].append({"voucher_type": "Sales Invoice", "voucher_no": "S2", "account": "Tax - X", "debit": 0, "credit": 10, "is_cancelled": 0})
    rows["Account"].append({"name": "Tax - X", "root_type": "Liability", "account_type": "Tax"})
    events = [e for e in extract(Erp(rows)).events if e.id.split(":")[0] in ("S2", "P1")]
    out = verify.ledger_check(Erp(rows), events)
    assert out["matched"] == 0 and len(out["unverifiable"]) == 2


# ── reliability: a measurement, not a decision ───────────────────────────────────────────────────────────
def test_a_perfect_record_of_three_is_not_reliable_and_the_model_s_own_report_is_not_counted():
    out = reliability.measure([reliability.Outcome("erp.comment.add", True)] * 3 + [reliability.Outcome("erp.comment.add", True, independent=False)] * 50)
    r = out["erp.comment.add"]
    assert (r["independent_outcomes"], r["not_independent"], r["decision_input"]) == (3, 50, "gather_evidence")


def test_reliable_needs_the_lower_bound_not_the_rate_and_a_failure_rate_shows_as_unreliable():
    good = reliability.measure([reliability.Outcome("a", True)] * 100)["a"]
    ok = reliability.measure([reliability.Outcome("a", True)] * 97 + [reliability.Outcome("a", False)] * 3)["a"]
    bad = reliability.measure([reliability.Outcome("a", True)] * 80 + [reliability.Outcome("a", False)] * 20)["a"]
    assert good["decision_input"] == "reliable" and good["lower_bound"] > 0.95
    assert ok["rate"] == 0.97 and ok["decision_input"] == "unreliable"                    # 97/100 has a lower bound below 0.95
    assert bad["decision_input"] == "unreliable"
    assert reliability.wilson_lower(0, 0) == 0.0


# ── the client: unreadable is an error for a strict caller, and a child table is asked for properly ─────────────
class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        if self._b is None:
            raise ValueError("no json")
        return self._b


def test_read_list_raises_where_get_list_returns_empty_and_names_the_status_only(monkeypatch):
    import httpx
    c = ErpnextClient(base_url="http://erp.test", api_key="k", api_secret="s")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(500, {"exception": "secret-looking text"}))
    with pytest.raises(ErpnextReadError) as e:
        c.read_list("Sales Invoice", ["name"])
    assert "HTTP 500" in str(e.value) and "secret-looking" not in str(e.value)
    assert c.get_list("Sales Invoice", ["name"]) == []


def test_a_child_table_is_read_with_its_parent_doctype(monkeypatch):
    import httpx
    seen = {}
    monkeypatch.setattr(httpx, "get", lambda url, **k: (seen.update(url=url, params=k["params"]), _Resp(200, {"data": [{"name": "x"}]}))[1])
    c = ErpnextClient(base_url="http://erp.test", api_key="k", api_secret="s")
    c.read_list("Sales Invoice Item", ["parent", "qty"])
    assert seen["params"]["parent"] == "Sales Invoice" and seen["url"].endswith("/api/resource/Sales%20Invoice%20Item")
    c.read_list("Sales Invoice", ["name"])
    assert "parent" not in seen["params"]


def test_two_payments_under_one_reference_or_a_fee_line_netted_into_the_sum_are_ambiguous_and_get_no_correction_proposal():
    rows = recon_rows(3800)
    rows["Payment Entry"].append({"name": "PAY-2", "docstatus": 1, "party": "Cedar", "paid_amount": 500, "reference_no": "WIRE-C1", "payment_type": "Receive"})
    d = reconcile.find(Erp(rows)).events[0]
    assert (d.posted, d.bank, d.payment, list(d.payments)) == (Decimal("4300.00"), Decimal("3800.00"), "", ["PAY-1", "PAY-2"]) and "2 payments" in d.ambiguous
    p = reconcile.propose(d).to_json()
    assert p["proposable"] is False and p["applied"] is False and p["action"].startswith("none")
    fee = recon_rows(3380)
    fee["Bank Transaction"].append({"name": "BT-2", "deposit": 0, "withdrawal": 420, "reference_number": "WIRE-C1", "docstatus": 1})
    d = reconcile.find(Erp(fee)).events[0]
    assert d.bank == Decimal("2960.00") and "withdrawal" in d.ambiguous and reconcile.propose(d).proposable is False
    ok = recon_rows(3380)
    assert reconcile.find(Erp(ok)).events[0].ambiguous == "" and reconcile.propose(reconcile.find(Erp(ok)).events[0]).proposable is True


def test_several_payments_that_add_up_to_the_bank_total_are_not_a_discrepancy():
    rows = recon_rows(3800)
    rows["Payment Entry"] = [{"name": "PAY-1", "docstatus": 1, "party": "Cedar", "paid_amount": 3300, "reference_no": "WIRE-C1", "payment_type": "Receive"},
                             {"name": "PAY-2", "docstatus": 1, "party": "Cedar", "paid_amount": 500, "reference_no": "WIRE-C1", "payment_type": "Receive"}]
    assert reconcile.find(Erp(rows)).events == []
