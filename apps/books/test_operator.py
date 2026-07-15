"""agentic-books as a Mission Runtime operator — proof over a fake ERPNext core.

Exercises the REAL core logic (fetch_activity KPIs + categorize/reconcile/close) and the
SDK-mount contract (GET /capabilities + POST /invoke) end-to-end, plus the Mission Runtime's
own HTTPOperatorClient driving the operator over the wire with exactly-once idempotency.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/books/test_operator.py -q
"""
from __future__ import annotations

import types
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from books import core
from books.operator import build_books_operator
from agentic_os.mission.operators import HTTPOperatorClient

# ── a fake ERPNext core (one open invoice + one matching deposit) ─────────────
SALES_INVOICES = [{
    "name": "ACC-SINV-001", "customer": "Acme Roofing", "grand_total": 4500,
    "outstanding_amount": 4500, "status": "Unpaid",
}]
PURCHASE_INVOICES = [{"name": "ACC-PINV-001", "supplier": "Beam Supply", "outstanding_amount": 1200}]
ACCOUNTS = [
    {"name": "Sales - SR", "root_type": "Income", "account_type": "Income Account"},
    {"name": "COGS - SR", "root_type": "Expense", "account_type": "Cost of Goods Sold"},
    {"name": "Bank - SR", "root_type": "Asset", "account_type": "Bank"},
]
GL_ENTRIES = [
    {"account": "Sales - SR", "debit": 0, "credit": 10000},
    {"account": "COGS - SR", "debit": 4000, "credit": 0},
    {"account": "Bank - SR", "debit": 6000, "credit": 0},
]
BANK_TXNS = [{
    "name": "ACC-BTN-001", "description": "Roofing job payment | seedtag",
    "deposit": 4500, "withdrawal": 0, "status": "Unreconciled",
    "unallocated_amount": 4500, "date": "2026-06-20", "docstatus": 1,
}]


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload or {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_get(url, timeout=None, headers=None, params=None):
    # erpnext_connected() — the cheap authenticated liveness probe.
    return _Resp(200, {"message": "Administrator"})


# resource path fragment -> the rows _get_list should return
_RESOURCE = {
    "Sales%20Invoice": SALES_INVOICES,
    "Purchase%20Invoice": PURCHASE_INVOICES,
    "Account": ACCOUNTS,
    "GL%20Entry": GL_ENTRIES,
    "Bank%20Transaction": BANK_TXNS,
}


class _FakeClient:
    """Stands in for httpx.Client — serves doctype lists on GET, records PUTs (categorize /
    journal-entry cancel) and POSTs (journal-entry create), returning a doc with a `name`."""
    puts: list[str] = []
    posts: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        for frag, rows in _RESOURCE.items():
            if url.endswith(f"/api/resource/{frag}"):
                return _Resp(200, {"data": rows})
        return _Resp(200, {"data": []})

    def put(self, url, headers=None, json=None):
        _FakeClient.puts.append(url)
        return _Resp(200, {"data": {}})

    def post(self, url, headers=None, json=None):
        _FakeClient.posts.append((url, json or {}))
        # ERPNext returns the created document (with its assigned name) under "data".
        return _Resp(200, {"data": {"name": "ACC-JV-2026-00001"}})


@pytest.fixture(autouse=True)
def _fake_erpnext(monkeypatch):
    _FakeClient.puts = []
    _FakeClient.posts = []
    core._CACHE.update(ts=0.0, data=None)  # no cache bleed between tests
    monkeypatch.setattr(core, "ERPNEXT_API_KEY", "test-key")
    monkeypatch.setattr(core, "httpx",
                        types.SimpleNamespace(get=_fake_get, Client=lambda timeout=None: _FakeClient()))
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_books_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"books.categorize", "books.reconcile", "books.close",
                         "books.record_revenue", "books.reverse_entry"}
    # only close is approval-gated (matches modules.yaml approval_required: [close])
    assert caps["books.close"]["approval_required"] is True
    assert caps["books.categorize"]["approval_required"] is False
    assert caps["books.reconcile"]["approval_required"] is False
    assert "close_staged" in caps["books.close"]["provides"]

    # onboarding: record_revenue provides revenue_recorded, has a compensating undo,
    # is NOT approval-gated (the saga backs it out rather than gating it).
    rr = caps["books.record_revenue"]
    assert "revenue_recorded" in rr["provides"]
    assert rr["undo"] == "books.reverse_entry"
    assert rr["approval_required"] is False
    assert rr["side_effecting"] is True
    # the compensating action is present too.
    assert caps["books.reverse_entry"]["side_effecting"] is True


def test_invoke_record_revenue_posts_journal_entry(client):
    res = client.post(
        "/invoke",
        json={"capability": "books.record_revenue",
              "inputs": {"subscription": "Pro plan", "amount": 250}},
    ).json()["result"]
    assert res["status"] == "done" and res["action"] == "record_revenue"
    assert res["revenue_recorded"] is True
    assert res["entry"] == "ACC-JV-2026-00001"  # name echoed back from the created doc
    # a real Journal Entry POST hit the (fake) ERPNext resource endpoint...
    assert len(_FakeClient.posts) == 1
    url, payload = _FakeClient.posts[0]
    assert url == "http://localhost:8092/api/resource/Journal%20Entry"
    # ...with a structurally valid, balanced double entry.
    assert payload["voucher_type"] == "Journal Entry"
    assert len(payload["accounts"]) == 2
    debit = sum(a["debit_in_account_currency"] for a in payload["accounts"])
    credit = sum(a["credit_in_account_currency"] for a in payload["accounts"])
    assert debit == credit == 250


def test_invoke_reverse_entry_cancels_journal_entry(client):
    res = client.post(
        "/invoke",
        json={"capability": "books.reverse_entry",
              "inputs": {"entry": "ACC-JV-2026-00001"}},
    ).json()["result"]
    assert res["status"] == "done" and res["action"] == "reverse_entry"
    assert res["reversed"] is True
    # the compensating action issued a cancel PUT against that Journal Entry.
    assert _FakeClient.puts == [
        "http://localhost:8092/api/resource/Journal%20Entry/ACC-JV-2026-00001"
    ]


def test_record_revenue_then_reverse_is_a_saga(client):
    """The onboarding mission's happy path then its compensating undo, end to end."""
    rec = client.post(
        "/invoke",
        json={"capability": "books.record_revenue", "inputs": {"amount": 99}},
    ).json()["result"]
    entry = rec["entry"]
    assert len(_FakeClient.posts) == 1 and entry

    rev = client.post(
        "/invoke",
        json={"capability": "books.reverse_entry", "inputs": {"entry": entry}},
    ).json()["result"]
    assert rev["reversed"] is True
    assert _FakeClient.puts == [
        f"http://localhost:8092/api/resource/Journal%20Entry/{entry}"
    ]


def test_invoke_categorize_hits_real_erpnext(client):
    res = client.post("/invoke", json={"capability": "books.categorize", "inputs": {}}).json()["result"]
    assert res["status"] == "done" and res["action"] == "categorize"
    assert res["result"]["transaction"] == "ACC-BTN-001"
    # the REAL core issued a PUT against the (fake) ERPNext for the uncategorized txn
    assert _FakeClient.puts == ["http://localhost:8092/api/resource/Bank%20Transaction/ACC-BTN-001"]


def test_reconcile_matches_deposit_to_invoice(client):
    res = client.post("/invoke", json={"capability": "books.reconcile", "inputs": {}}).json()["result"]
    assert res["status"] == "done" and res["action"] == "reconcile"
    assert res["match"]["invoice"] == "ACC-SINV-001"
    assert res["match"]["deposit"] == "ACC-BTN-001"
    assert res["match"]["amount"] == "$4,500"
    assert _FakeClient.puts == []  # read-only: reconcile only stages the match


def test_close_is_approval_gated(client):
    res = client.post("/invoke", json={"capability": "books.close", "inputs": {}}).json()["result"]
    assert res["status"] == "pending_approval" and res["requires"] == "human approval"
    assert res["close_pct"] == 50  # 3 of 6 checklist items done (1 txn still uncategorized)
    assert _FakeClient.puts == []  # never posts the Period Closing Voucher


def test_idempotency_dedupes_side_effect(client):
    body = {"capability": "books.categorize", "inputs": {}, "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second
    # exactly-once: the categorize PUT hit ERPNext a single time
    assert _FakeClient.puts == ["http://localhost:8092/api/resource/Bank%20Transaction/ACC-BTN-001"]


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"books": "http://books"}, transport=_transport)
    rec = oc.invoke("books", "books.reconcile", {}, idempotency_key="m-1")
    assert rec["status"] == "done" and rec["match"]["invoice"] == "ACC-SINV-001"

    clo = oc.invoke("books", "books.close", {}, idempotency_key="m-2")
    assert clo["status"] == "pending_approval" and clo["close_pct"] == 50
