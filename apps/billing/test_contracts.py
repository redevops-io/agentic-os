"""Declared-output, undo-input and declaration contracts for the billing operator (counting fake Lago)."""
from __future__ import annotations

import types

import pytest

from agentic_os.mission.compensation import Reversibility, lint_capability, registry_problems, reversibility_of
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.verify import CompositeVerifier
from billing import core
from billing.operator import build_billing_operator

APPR = {"_approval": {"approved": True}}
INVOICES = [{"number": "INV-001", "customer": {"name": "X"}, "total_amount_cents": 1000, "status": "finalized",
             "payment_status": "failed", "payment_overdue": True, "lago_id": "lago-1",
             "issuing_date": "2026-05-15"}]


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p, self.text = status, payload or {}, ""

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class _Lago:
    """Counting fake backend: records every mutating call."""
    calls: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        if url.endswith("/api/v1/invoices"):
            return _Resp(200, {"invoices": INVOICES, "meta": {"total_pages": 1}})
        if "/api/v1/invoices/lago-1" in url:
            return _Resp(200, {"invoice": {"fees": [{"lago_id": "fee-1", "amount_cents": 1000}]}})
        return _Resp(200, {"customers": [], "meta": {"total_pages": 1}})

    def post(self, url, headers=None, json=None):
        _Lago.calls.append(("POST", url, json))
        if url.endswith("/api/v1/credit_notes"):
            return _Resp(200, {"credit_note": {"lago_id": "cn-1"}})
        if url.endswith("/api/v1/subscriptions"):
            return _Resp(200, {"subscription": {"lago_id": "lago-sub-1"}})
        return _Resp(200)

    def delete(self, url, headers=None):
        _Lago.calls.append(("DELETE", url, None))
        return _Resp(200, {})


@pytest.fixture(autouse=True)
def _lago(monkeypatch):
    _Lago.calls = []
    core._CACHE.update(ts=0.0, data=None)
    monkeypatch.setattr(core, "LAGO_API_KEY", "k")
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(get=lambda *a, **k: _Resp(200),
                                                             Client=lambda timeout=None: _Lago()))


@pytest.fixture()
def rig():
    op = build_billing_operator()
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    return op, reg, CompositeVerifier(reg, lambda m: {"*"})


def _verify(rig, cap, result):
    _, _, v = rig
    node = types.SimpleNamespace(capability=cap, side_effecting=True, assertions=[])
    return v.verify(node, result, None, None)


def _mutations(method=None):
    return [c for c in _Lago.calls if method is None or c[0] == method]


def test_create_subscription_result_passes_verifier(rig):
    res = rig[0].invoke("billing.create_subscription", {"customer": "acme", "plan_code": "pro", **APPR})
    assert res["status"] == "done" and _verify(rig, "billing.create_subscription", res).decision.name == "ACCEPT"
    assert len(_mutations("POST")) == 1


def test_refund_result_passes_verifier(rig):
    res = rig[0].invoke("billing.refund", {"invoice": "INV-001", "amount": "$10", **APPR})
    assert res["status"] == "done" and _verify(rig, "billing.refund", res).decision.name == "ACCEPT"
    assert len(_mutations("POST")) == 1


def test_forward_verify_undo_round_trip_targets_the_created_subscription(rig):
    op = rig[0]
    fwd = op.invoke("billing.create_subscription", {"customer": "acme", "plan_code": "pro", **APPR})
    assert _verify(rig, "billing.create_subscription", fwd).decision.name == "ACCEPT"
    undo_inputs = {"undo_of": "billing.create_subscription", "result": fwd, "_node": "n1", "_mission": "m1", **APPR}
    undone = op.invoke("billing.cancel_subscription", undo_inputs)
    assert undone["status"] == "done" and undone["cancelled"] is True
    assert _verify(rig, "billing.cancel_subscription", undone).decision.name == "ACCEPT"
    deletes = _mutations("DELETE")
    assert len(deletes) == 1 and deletes[0][1].endswith("/api/v1/subscriptions/" + fwd["subscription_id"])


@pytest.mark.parametrize("inputs", [{}, {"result": {}}, {"result": {"subscription_id": ""}},
                                    {"customer": "acme"},
                                    {"result": {"subscription_id": "x/../other"}},
                                    {"subscription_id": "a/b"}])
def test_undo_refuses_without_a_valid_subscription_id(rig, inputs):
    res = rig[0].invoke("billing.cancel_subscription", {**inputs, **APPR})
    assert res["status"] == "error" and res["cancelled"] is False
    assert _mutations() == []


def test_declared_outputs_are_returned_on_failure_paths_too(rig, monkeypatch):
    class _Reject(_Lago):
        def post(self, url, headers=None, json=None):
            return _Resp(422, {})
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(get=lambda *a, **k: _Resp(200),
                                                             Client=lambda timeout=None: _Reject()))
    res = rig[0].invoke("billing.create_subscription", {"customer": "acme", "plan_code": "pro", **APPR})
    assert res["status"] == "error" and res["subscription"] is False


@pytest.mark.parametrize("sid", ["x/../other", "a/b", "a b", "..", ""])
def test_valid_id_rejects_path_like_values(sid):
    assert core.valid_id(sid) is False


def test_capability_declarations_pass_lint(rig):
    _, reg, _ = rig
    caps = reg.all()
    assert [lint_capability(c) for c in caps if lint_capability(c)] == []
    assert registry_problems(caps, reg.get, require_verify=False) == []
    by = {c.name: c for c in caps}
    # money out and customer email are irreversible; only the subscription pair is explicitly compensatable
    for name in ("billing.dunning", "billing.refund", "billing.cancel_subscription"):
        assert reversibility_of(by[name]) is Reversibility.IRREVERSIBLE
    assert by["billing.create_subscription"].undo == "billing.cancel_subscription"


@pytest.mark.parametrize("amount", ["1e999999", "-1e999999", "1E400", "9" * 40])
def test_absurd_refund_amounts_are_rejected_not_raised(amount):
    with pytest.raises(ValueError):
        core.parse_amount_cents(amount)
    r = rig_refund(amount)
    assert r["status"] == "error" and _mutations() == []


def rig_refund(amount):
    return build_billing_operator().invoke("billing.refund", {"invoice": "INV-001", "amount": amount, **APPR})


@pytest.mark.parametrize("bad", [{}, {"customer": "acme"}, {"plan_code": "pro"}, {"customer": {"a": 1}, "plan_code": "pro"},
                                 {"customer": ["a"], "plan_code": "pro"}, {"customer": "acme", "plan_code": "a/b"},
                                 {"customer": "", "plan_code": "pro"}])
def test_create_subscription_requires_explicit_customer_and_plan(rig, bad):
    res = rig[0].invoke("billing.create_subscription", {**bad, **APPR})
    assert res["status"] == "error" and res["subscription"] is False
    assert _mutations() == []


def test_undo_never_cancels_a_subscription_that_already_existed(rig, monkeypatch):
    class _Existing(_Lago):
        def get(self, url, headers=None, params=None):
            if "/api/v1/subscriptions/" in url:
                return _Resp(200, {"subscription": {"external_id": "x"}})
            return super().get(url, headers, params)
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(get=lambda *a, **k: _Resp(200),
                                                             Client=lambda timeout=None: _Existing()))
    op = rig[0]
    fwd = op.invoke("billing.create_subscription", {"customer": "acme", "plan_code": "pro", **APPR})
    assert fwd["status"] == "done" and fwd["created"] is False and _mutations() == []
    assert _verify(rig, "billing.create_subscription", fwd).decision.name == "ACCEPT"
    undone = op.invoke("billing.cancel_subscription", {"undo_of": "billing.create_subscription", "result": fwd, **APPR})
    assert undone["status"] == "error" and undone["cancelled"] is False
    assert _mutations() == []


def test_refund_amount_cents_is_explicit_and_checked():
    ok = rig_refund_inputs({"amount_cents": 500})
    assert ok["status"] == "done" and ok["refunded_cents"] == 500
    assert rig_refund_inputs({"amount_cents": 500, "amount": "$5"})["status"] == "done"
    for bad in ({"amount_cents": 500, "amount": "$6"}, {"amount_cents": "500"}, {"amount_cents": 5.5},
                {"amount_cents": True}, {"amount_cents": 0}, {"amount_cents": -1}):
        assert rig_refund_inputs(bad)["status"] == "error"
    assert len(_mutations("POST")) == 2


def rig_refund_inputs(extra):
    return build_billing_operator().invoke("billing.refund", {"invoice": "INV-001", **extra, **APPR})


def test_refund_currency_must_match_the_invoice(monkeypatch):
    monkeypatch.setitem(INVOICES[0], "currency", "EUR")
    assert rig_refund_inputs({"amount_cents": 100, "currency": "USD"})["status"] == "error"
    assert rig_refund_inputs({"amount_cents": 100, "currency": "usd1"})["status"] == "error"
    assert _mutations() == []
    ok = rig_refund_inputs({"amount_cents": 100, "currency": "eur"})
    assert ok["status"] == "done" and ok["currency"] == "EUR"
