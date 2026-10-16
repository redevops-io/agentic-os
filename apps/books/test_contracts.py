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
    journal: list = []          # rows served for Journal Entry lookups (idempotency)
    fail_submit = False

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
        if url.endswith("/api/resource/Journal%20Entry"):
            return _Resp(200, {"data": _Erp.journal})
        if "/api/resource/Journal%20Entry/" in url and _Erp.posts:
            return _Resp(200, {"data": {"accounts": _Erp.posts[-1][1]["accounts"]}})
        return _Resp(200, {"data": []})

    def put(self, url, headers=None, json=None):
        _Erp.puts.append((url, json or {}))
        if _Erp.fail_submit and (json or {}).get("docstatus") == 1:
            return _Resp(500, {})
        return _Resp(200, {"data": {"docstatus": (json or {}).get("docstatus")}})

    def post(self, url, headers=None, json=None):
        _Erp.posts.append((url, json or {}))
        return _Resp(200, {"data": {"name": "ACC-JV-1"}})


@pytest.fixture(autouse=True)
def _erp(monkeypatch):
    _Erp.posts, _Erp.puts, _Erp.sales, _Erp.journal, _Erp.fail_submit = [], [], [], [], False
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


@pytest.mark.parametrize("inputs", [{}, {"result": {}}, {"result": {"entry": ""}}, {"entry": "X/../Y"},
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


def test_same_idempotency_key_posts_one_entry(rig):
    inp = {"amount": "50", "idempotency_key": "m1:n1", **APPR}
    first = rig[0].invoke("books.record_revenue", dict(inp))
    assert first["status"] == "done" and len(_Erp.posts) == 1
    token = [w for w in _Erp.posts[0][1]["user_remark"].split() if w.startswith("[idem:")][0].strip("[]")
    _Erp.journal = [{"name": first["entry"], "docstatus": 1, "user_remark": _Erp.posts[0][1]["user_remark"]}]
    again = rig[0].invoke("books.record_revenue", dict(inp))
    assert again["status"] == "done" and again["entry"] == first["entry"] and again["deduplicated"] is True
    assert len(_Erp.posts) == 1 and token.startswith("idem:")
    # the same key with a different amount is a conflict, never a silent dedupe
    other = rig[0].invoke("books.record_revenue", {**inp, "amount": "51"})
    assert other["status"] == "error" and other["error_type"] == "idempotency_conflict" and len(_Erp.posts) == 1
    assert _verify(rig, "books.record_revenue", again).decision.name == "ACCEPT"


def test_mission_and_node_derive_the_idempotency_key(rig):
    rig[0].invoke("books.record_revenue", {"amount": "5", "_mission": "m1", "_node": "n1", **APPR})
    assert "[idem:" in _Erp.posts[0][1]["user_remark"]


def test_failed_submit_retains_the_draft_without_claiming_success(rig):
    _Erp.fail_submit = True
    res = rig[0].invoke("books.record_revenue", {"amount": "5", "idempotency_key": "k", **APPR})
    assert res["status"] == "error" and res["executed"] is False and res["revenue_recorded"] is False
    assert "entry" not in res and res["retained"]["name"] == "ACC-JV-1"
    assert [p for p in _Erp.puts if p[1] == {"docstatus": 2}] == []        # never auto-deleted/cancelled
    # a retry with the same key resumes the retained draft instead of posting a second one
    _Erp.fail_submit = False
    _Erp.journal = [{"name": "ACC-JV-1", "docstatus": 0, "user_remark": _Erp.posts[0][1]["user_remark"]}]
    changed = rig[0].invoke("books.record_revenue", {"amount": "7", "idempotency_key": "k", **APPR})
    assert changed["error_type"] == "idempotency_conflict" and _Erp.puts == [(_Erp.puts[0][0], {"docstatus": 1})]
    retry = rig[0].invoke("books.record_revenue", {"amount": "5", "idempotency_key": "k", **APPR})
    assert retry["status"] == "done" and retry["entry"] == "ACC-JV-1" and len(_Erp.posts) == 1


def test_currency_must_match_the_ledger(rig):
    res = rig[0].invoke("books.record_revenue", {"amount": "5", "currency": "ZZZ", **APPR})
    assert res["status"] == "error" and _Erp.posts == []
    assert rig[0].invoke("books.record_revenue", {"amount": "5", "currency": core.COMPANY_CURRENCY.lower(), **APPR}
                         )["status"] == "done"


def test_resumed_draft_with_a_different_amount_is_not_submitted(rig):
    _Erp.fail_submit = True
    rig[0].invoke("books.record_revenue", {"amount": "7", "idempotency_key": "k2", **APPR})
    remark = _Erp.posts[0][1]["user_remark"]
    _Erp.fail_submit = False
    _Erp.journal = [{"name": "ACC-JV-1", "docstatus": 0, "user_remark": remark}]
    _Erp.posts[0][1]["accounts"][0]["debit_in_account_currency"] = 700.0      # draft edited out of band
    _Erp.puts.clear()
    res = rig[0].invoke("books.record_revenue", {"amount": "7", "idempotency_key": "k2", **APPR})
    assert res["status"] == "error" and res["error_type"] == "idempotency_conflict" and _Erp.puts == []


def test_keys_are_scoped_to_tenant(rig):
    rig[0].invoke("books.record_revenue", {"amount": "5", "idempotency_key": "same", "_tenant": "a", **APPR})
    rig[0].invoke("books.record_revenue", {"amount": "5", "idempotency_key": "same", "_tenant": "b", **APPR})
    remarks = [p[1]["user_remark"] for p in _Erp.posts]
    assert remarks[0] != remarks[1]


def test_concurrent_duplicate_is_detected_after_create(rig):
    _Erp.journal = []
    orig = _Erp.post

    def post(self, url, headers=None, json=None):
        r = orig(self, url, headers, json)
        _Erp.journal = [{"name": "ACC-JV-1"}, {"name": "ACC-JV-2"}]      # a racing writer created another
        return r
    _Erp.post = post
    try:
        res = rig[0].invoke("books.record_revenue", {"amount": "5", "idempotency_key": "r", **APPR})
    finally:
        _Erp.post = orig
    assert res["status"] == "error" and res["error_type"] == "duplicate_entry" and _Erp.puts == []
