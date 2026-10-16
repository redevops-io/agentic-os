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
                                    {"result": {"subscription_id": "../customers/C1"}},
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


@pytest.mark.parametrize("sid", ["../customers/C1", "a/b", "a b", "..", ""])
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
