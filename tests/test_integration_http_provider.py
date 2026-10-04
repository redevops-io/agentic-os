"""Phase 3 (public) — the IntegrationProvider contract against a real REST shape (ERPNext/Frappe).

Proves the contract isn't over-fit to the in-memory fake: CRUD + search + 404 + normalized errors work over HTTP
(offline, via a fake Frappe transport), AND the Obligation engine discharges against this real-provider shape —
including catching a silent write failure (a 200 that doesn't persist).
"""
from __future__ import annotations

from agentic_os.integration import (
    IntegrationError, IntegrationProvider, Obligation, ObligationEngine, ObligationStatus, RetryPolicy,
)
from agentic_os.integration.contracts import ExceptionCategory
from agentic_os.integration.provider import IntegrationErrorCode
from agentic_os.integration.providers import ErpnextIntegrationProvider


class _FakeFrappe:
    """Minimal Frappe REST: /api/resource/{doctype}[/{name}], {data:...} envelope, token auth."""

    def __init__(self, *, drop_puts: bool = False, unauth: bool = False):
        self.store: dict[str, dict[str, dict]] = {}
        self.drop_puts = drop_puts
        self.unauth = unauth
        self._seq = 0

    def __call__(self, method, url, headers, body):
        if self.unauth or not (headers or {}).get("Authorization"):
            return 401, {}
        if "/api/resource/" not in url:
            return 200, {}                               # health ping on the base URL
        path = url.split("/api/resource/", 1)[1]
        parts = path.split("/")
        doctype = parts[0]
        name = parts[1] if len(parts) > 1 else ""
        tbl = self.store.setdefault(doctype, {})
        if method == "GET":
            if name:
                return (200, {"data": {"name": name, **tbl[name]}}) if name in tbl else (404, {})
            return 200, {"data": [{"name": n, **f} for n, f in tbl.items()]}
        if method == "POST":
            self._seq += 1
            nm = body.get("name") or f"{doctype}-{self._seq:03d}"
            tbl[nm] = dict(body)
            return 200, {"data": {"name": nm, **body}}
        if method == "PUT":
            if self.drop_puts:
                return 200, {"data": {"name": name, **tbl.get(name, {})}}   # 200 but NOT persisted
            tbl[name] = {**tbl.get(name, {}), **body}
            return 200, {"data": {"name": name, **tbl[name]}}
        return 405, {}


def _erp(**kw):
    return ErpnextIntegrationProvider("https://erp.example", token="key:secret", transport=_FakeFrappe(**kw))


def test_satisfies_contract_and_capabilities():
    p = _erp()
    assert isinstance(p, IntegrationProvider)
    assert "billing.invoice.create" in p.describe_capabilities() and p.health().healthy


def test_crud_roundtrip_with_field_normalization():
    p = _erp()
    # create a customer (canonical fields → Frappe fields → back)
    res = p.create_object("customer", {"name": "ACME Inc", "email": "ap@acme.com", "group": "Enterprise"})
    assert res.ok
    cust = p.read_object("customer", res.external_id)
    assert cust.normalized_fields["name"] == "ACME Inc" and cust.normalized_fields["email"] == "ap@acme.com"

    # create an invoice, read it back with amount/currency normalized from grand_total
    inv = p.create_object("invoice", {"customer": "ACME Inc", "amount": 1300, "currency": "usd", "status": "Draft"})
    got = p.read_object("invoice", inv.external_id)
    assert got.normalized_fields["amount"] == 1300 and got.normalized_fields["status"] == "Draft"


def test_search_and_404():
    p = _erp()
    p.create_object("customer", {"name": "ACME", "email": "x@acme.com"})
    found = p.search_objects("customer", {"email": "x@acme.com"})
    assert len(found) == 1 and found[0].normalized_fields["name"] == "ACME"
    assert p.read_object("customer", "does-not-exist") is None


def test_normalized_errors():
    p = _erp(unauth=True)
    try:
        p.read_object("customer", "c1")
        assert False, "expected IntegrationError"
    except IntegrationError as e:
        assert e.code == IntegrationErrorCode.AUTH_EXPIRED


def test_obligation_discharges_against_http_provider():
    p = _erp()
    inv = p.create_object("invoice", {"customer": "ACME", "amount": 1300, "currency": "usd", "status": "Draft"})
    obl = Obligation(trigger="billing.invoice.paid", destination_resource="erpnext",
                     retry_policy=RetryPolicy(max_attempts=1),
                     expected_state={"invoice": {"status": "Paid"}})

    def _mark_paid():
        return p.update_object("invoice", inv.external_id, {"status": "Paid"}, idempotency_key=inv.external_id)

    res = ObligationEngine().discharge(obl, p, action=_mark_paid, targets={"invoice": inv.external_id})
    assert res.satisfied and res.obligation.status == ObligationStatus.SATISFIED
    assert res.receipt.observed_state["invoice"]["status"] == "Paid"


def test_silent_http_failure_is_caught_against_real_provider():
    # PUT returns 200 but the change is not persisted → the obligation catches it via read-back
    p = _erp(drop_puts=True)
    inv = p.create_object("invoice", {"customer": "ACME", "amount": 1300, "currency": "usd", "status": "Draft"})
    obl = Obligation(trigger="billing.invoice.paid", destination_resource="erpnext",
                     retry_policy=RetryPolicy(max_attempts=1), expected_state={"invoice": {"status": "Paid"}})
    res = ObligationEngine().discharge(
        obl, p, action=lambda: p.update_object("invoice", inv.external_id, {"status": "Paid"}),
        targets={"invoice": inv.external_id})
    assert not res.satisfied and res.exception.category == ExceptionCategory.OBLIGATION_CONFLICT
