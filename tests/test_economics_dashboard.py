"""The economics dashboard reads the same ERPNext documents as the finance dashboard and shows the same numbers a person would compute from them."""
from tests.test_dashboards import _app, _metric, _operator, _panel
from tests.test_economics import Erp, base_rows, recon_rows


def _dash(monkeypatch, tmp_path, rows, **kw):
    c, svc, _ = _app(monkeypatch, tmp_path)
    _operator(c)
    svc.config.sources.update({"erpnext": {"project": "billing"}})
    svc.ctx.clients["economics"] = Erp({**rows, **recon_rows(3380)}, **kw)
    svc.cache.invalidate()
    return c, c.get("/api/dashboards/economics", params={"days": 200}).json()


def test_the_dashboard_is_offered_when_erpnext_is_enabled_and_shows_profit_discounts_cancellations_and_the_bank_mismatch(monkeypatch, tmp_path):
    c, dash = _dash(monkeypatch, tmp_path, base_rows())
    assert "economics" in {d["id"] for d in c.get("/api/dashboards").json()["dashboards"]}
    assert _metric(dash, "unprofitable")["value"] == 1                                       # Birch, once rework is counted
    assert _metric(dash, "discounts")["value"] == 1110.0 and _metric(dash, "discounts_unref")["value"] == 910.0
    assert _metric(dash, "cancel_cost")["value"] == 1000.0 and _metric(dash, "mismatch")["value"] == 1
    cust = {r["cells"]["customer"]: r["cells"] for r in _panel(dash, "customers")["rows"]}
    assert (cust["Acme"]["profit"], cust["Birch"]["profit"]) == ("1700.00", "-70.00")
    disc = {r["cells"]["invoice"]: r["cells"] for r in _panel(dash, "discount_rows")["rows"]}
    assert disc["S2"]["claimed_by"] == "usr-cfo" and disc["S3"]["claim"] == "no claim"
    sup = [r["cells"]["supplier"] for r in _panel(dash, "supplier_rows")["rows"]]
    assert sup == ["Quartz", "Northwind"]
    assert _panel(dash, "mismatch_rows")["rows"][0]["cells"]["difference"] == 420.0
    canc = {r["cells"]["invoice"]: r["cells"] for r in _panel(dash, "cancellation_rows")["rows"]}
    assert (canc["S5"]["forgone"], canc["S5"]["outcome"].startswith("not replaced")) == ("1000.00", True)
    assert (canc["S6"]["forgone"], canc["S6"]["outcome"]) == ("0.00", "replaced by S6-1")
    appr = {r["cells"]["approver"]: r["cells"]["amount"] for r in _panel(dash, "approver_rows")["rows"]}
    assert appr == {"usr-cfo": "200.00", "no approval claim": "910.00"}
    assert "ref" not in _panel(dash, "customers")["rows"][0] or not _panel(dash, "customers")["rows"][0]["ref"]      # an aggregate row names no single document
    assert "S1" in _panel(dash, "customers")["rows"][0]["cells"]["documents"] or "S1" in str([r["cells"]["documents"] for r in _panel(dash, "customers")["rows"]])


def test_a_corrected_payment_leaves_no_open_mismatch_and_its_history_is_shown(monkeypatch, tmp_path):
    rows = {**base_rows(), **recon_rows(3380)}
    rows["Payment Entry"] = [{"name": "PAY-1", "docstatus": 2, "party": "Cedar", "paid_amount": 3800, "reference_no": "WIRE-C1", "payment_type": "Receive"},
                             {"name": "PAY-2", "docstatus": 1, "party": "Cedar", "paid_amount": 3380, "reference_no": "WIRE-C1", "payment_type": "Receive"}]
    rows["Payment Entry Reference"] = [{"parent": "PAY-2", "reference_doctype": "Sales Invoice", "reference_name": "S5"}]
    c, svc, _ = _app(monkeypatch, tmp_path)
    _operator(c)
    svc.config.sources.update({"erpnext": {"project": "billing"}})
    svc.ctx.clients["economics"] = Erp(rows)
    svc.cache.invalidate()
    dash = c.get("/api/dashboards/economics", params={"days": 200}).json()
    assert _metric(dash, "mismatch")["value"] == 0 and _panel(dash, "mismatch_rows")["rows"] == []
    k = _panel(dash, "correction_rows")["rows"][0]["cells"]
    assert (k["was"], k["now"], k["bank"], k["status"], k["replacement"]) == (3800.0, 3380.0, 3380.0, "reconciled", "PAY-2")


def test_a_source_that_could_not_be_fully_read_makes_the_dashboard_say_so(monkeypatch, tmp_path):
    _, dash = _dash(monkeypatch, tmp_path, base_rows(), fail=["Purchase Invoice"])
    st = next(s for s in dash["sources"] if s["id"] == "economics")
    assert st["state"] != "ok" and "Purchase Invoice" in st["detail"] and "incomplete" in st["detail"]


def test_a_site_without_the_optional_custom_fields_is_not_reported_as_stale_but_says_what_it_lacks(monkeypatch, tmp_path):
    rows = base_rows()
    for r in rows["Timesheet Detail"]:
        r.pop("custom_supplier"), r.pop("custom_reference")
    _, dash = _dash(monkeypatch, tmp_path, rows)
    st = next(s for s in dash["sources"] if s["id"] == "economics")
    assert st["state"] == "ok" and "custom_supplier" in st["detail"] and st["detail"].startswith("optional")
