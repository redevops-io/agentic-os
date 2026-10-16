"""Declared-output, undo-input, amount-validation and declaration contracts for the books operator."""
from __future__ import annotations

import types

import pytest

from agentic_os.mission.compensation import Reversibility, lint_capability, registry_problems, reversibility_of
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.verify import CompositeVerifier
from books import core
from books.operator import build_books_operator

APPR = {"_approval": {"approved": True}}
ACCOUNTS = [{"name": "Sales - MWM", "root_type": "Income", "account_type": "Income Account"},
            {"name": "Bank - MWM", "root_type": "Asset", "account_type": "Bank"}]
BANK_TXNS = [{"name": "ACC-BTN-001", "description": "Fee | seedtag", "deposit": 4500, "withdrawal": 0,
              "status": "Unreconciled", "unallocated_amount": 4500, "date": "2026-06-20", "docstatus": 1}]


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload or {}

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class _Erp:
    """Counting fake ERPNext: records every POST and PUT."""
    posts: list = []
    puts: list = []
    sales: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        if url.endswith("/api/resource/Account"):
            return _Resp(200, {"data": ACCOUNTS})
        if url.endswith("/api/resource/Bank%20Transaction"):
            return _Resp(200, {"data": BANK_TXNS})
        if url.endswith("/api/resource/Sales%20Invoice"):
            return _Resp(200, {"data": _Erp.sales})
        return _Resp(200, {"data": []})

    def put(self, url, headers=None, json=None):
        _Erp.puts.append((url, json or {}))
        return _Resp(200, {"data": {"docstatus": (json or {}).get("docstatus")}})

    def post(self, url, headers=None, json=None):
        _Erp.posts.append((url, json or {}))
        return _Resp(200, {"data": {"name": "ACC-JV-1"}})


@pytest.fixture(autouse=True)
def _erp(monkeypatch):
    _Erp.posts, _Erp.puts, _Erp.sales = [], [], []
    core._CACHE.update(ts=0.0, data=None)
    monkeypatch.setattr(core, "ERPNEXT_API_KEY", "k")
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(
        get=lambda *a, **k: _Resp(200, {"message": "ok"}), Client=lambda timeout=None: _Erp()))


@pytest.fixture()
def rig():
    op = build_books_operator()
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    return op, reg, CompositeVerifier(reg, lambda m: {"*"})


def _verify(rig, cap, result):
    node = types.SimpleNamespace(capability=cap, side_effecting=True, assertions=[])
    return rig[2].verify(node, result, None, None)


def test_categorize_result_passes_verifier(rig):
    res = rig[0].invoke("books.categorize", dict(APPR))
    assert res["status"] == "done" and _verify(rig, "books.categorize", res).decision.name == "ACCEPT"
    assert len(_Erp.puts) == 1


def test_forward_verify_undo_round_trip_reverses_the_same_entry(rig):
    op = rig[0]
    fwd = op.invoke("books.record_revenue", {"subscription": "Pro", "amount": "250.00", **APPR})
    assert fwd["status"] == "done" and _verify(rig, "books.record_revenue", fwd).decision.name == "ACCEPT"
    undone = op.invoke("books.reverse_entry", {"undo_of": "books.record_revenue", "result": fwd,
                                               "_node": "n1", "_mission": "m1", **APPR})
    assert undone["status"] == "done" and undone["reversed"] is True and undone["entry"] == fwd["entry"]
    assert _verify(rig, "books.reverse_entry", undone).decision.name == "ACCEPT"
    cancels = [p for p in _Erp.puts if p[1] == {"docstatus": 2}]
    assert len(cancels) == 1 and cancels[0][0].endswith("/Journal%20Entry/" + fwd["entry"])


@pytest.mark.parametrize("inputs", [{}, {"result": {}}, {"result": {"entry": ""}}, {"entry": "../Sales Invoice/X"},
                                    {"result": {"entry": "A/B"}}])
def test_undo_refuses_without_a_valid_entry_id(rig, inputs):
    res = rig[0].invoke("books.reverse_entry", {**inputs, **APPR})
    assert res["status"] == "error" and res["reversed"] is False
    assert _Erp.puts == [] and _Erp.posts == []


@pytest.mark.parametrize("amount", [None, "", 0, "0", -5, "-0.01", "nan", "NaN", "inf", "-inf", "1e308", "abc",
                                    True, [], {}, "0.001", 10 ** 12])
def test_record_revenue_rejects_missing_or_invalid_amount(rig, amount):
    inp = {"subscription": "Pro", **APPR}
    if amount is not None:
        inp["amount"] = amount
    res = rig[0].invoke("books.record_revenue", inp)
    assert res["status"] == "error" and res["revenue_recorded"] is False
    assert _Erp.posts == [] and _Erp.puts == []


def test_record_revenue_rounds_to_minor_units(rig):
    res = rig[0].invoke("books.record_revenue", {"amount": "10.005", **APPR})
    assert res["status"] == "done"
    accts = _Erp.posts[0][1]["accounts"]
    assert accts[0]["debit_in_account_currency"] == 10.01 and accts[1]["credit_in_account_currency"] == 10.01


def test_reconcile_compares_to_the_cent():
    _Erp.sales = [{"name": "SI-1", "customer": "C", "grand_total": 4500.4, "outstanding_amount": 4500.40,
                   "status": "Unpaid"}]
    assert "match" not in core.reconcile()
    _Erp.sales = [{"name": "SI-1", "customer": "C", "grand_total": 4500, "outstanding_amount": 4500,
                   "status": "Unpaid"}]
    core._CACHE.update(ts=0.0, data=None)
    assert core.reconcile()["match"]["invoice"] == "SI-1"


def test_capability_declarations_pass_lint(rig):
    _, reg, _ = rig
    caps = reg.all()
    assert [lint_capability(c) for c in caps if lint_capability(c)] == []
    assert registry_problems(caps, reg.get, require_verify=False) == []
    by = {c.name: c for c in caps}
    assert reversibility_of(by["books.close"]) is Reversibility.IRREVERSIBLE
    assert by["books.record_revenue"].undo == "books.reverse_entry"
