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
    "name": "ACC-SINV-001", "customer": "Whitfield Family Trust", "grand_total": 4500,
    "outstanding_amount": 4500, "status": "Unpaid",
}]
PURCHASE_INVOICES = [{"name": "ACC-PINV-001", "supplier": "Bloomberg Data Services", "outstanding_amount": 1200}]
ACCOUNTS = [
    {"name": "Sales - MWM", "root_type": "Income", "account_type": "Income Account"},
    {"name": "COGS - MWM", "root_type": "Expense", "account_type": "Cost of Goods Sold"},
    {"name": "Bank - MWM", "root_type": "Asset", "account_type": "Bank"},
]
GL_ENTRIES = [
    {"account": "Sales - MWM", "debit": 0, "credit": 10000},
    {"account": "COGS - MWM", "debit": 4000, "credit": 0},
    {"account": "Bank - MWM", "debit": 6000, "credit": 0},
]
BANK_TXNS = [{
    "name": "ACC-BTN-001", "description": "Advisory fee payment | seedtag",
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
    put_bodies: list = []
    posts: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    drafts: dict = {}   # frag -> draft rows served when the query filters docstatus = 0

    def get(self, url, headers=None, params=None):
        import json as _j
        flt = _j.loads((params or {}).get("filters") or "[]")
        is_draft = ["docstatus", "=", 0] in flt
        for frag, rows in {**_RESOURCE, "Journal%20Entry": []}.items():
            if url.endswith(f"/api/resource/{frag}"):
                if is_draft:
                    return _Resp(200, {"data": _FakeClient.drafts.get(frag, [])})
                return _Resp(200, {"data": rows})
        return _Resp(200, {"data": []})

    def put(self, url, headers=None, json=None):
        _FakeClient.puts.append(url)
        _FakeClient.put_bodies.append((url, json or {}))
        if "Journal%20Entry" in url:        # ERPNext echoes the document with its new docstatus
            return _Resp(200, {"data": {"docstatus": (json or {}).get("docstatus")}})
        return _Resp(200, {"data": {}})

    def post(self, url, headers=None, json=None):
        _FakeClient.posts.append((url, json or {}))
        # ERPNext returns the created document (with its assigned name) under "data".
        return _Resp(200, {"data": {"name": "ACC-JV-2026-00001"}})


@pytest.fixture(autouse=True)
def _fake_erpnext(monkeypatch):
    _FakeClient.puts = []
    _FakeClient.put_bodies = []
    _FakeClient.posts = []
    _FakeClient.drafts = {}
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
    # categorize (ERPNext write) and close are approval-gated
    assert caps["books.close"]["approval_required"] is True
    assert caps["books.categorize"]["approval_required"] is True
    assert caps["books.reconcile"]["approval_required"] is False
    assert "close_staged" in caps["books.close"]["provides"]

    # onboarding: record_revenue provides revenue_recorded, has a compensating undo,
    # is approval-gated (it posts to the GL) and the saga can still back it out.
    rr = caps["books.record_revenue"]
    assert "revenue_recorded" in rr["provides"]
    assert rr["undo"] == "books.reverse_entry"
    assert rr["approval_required"] is True
    assert rr["side_effecting"] is True
    # the compensating action is present too, and gated.
    assert caps["books.reverse_entry"]["side_effecting"] is True
    assert caps["books.reverse_entry"]["approval_required"] is True


APPR = {"_approval": {"approved": True}}


def _op(cap, inputs):
    """Trusted in-process path: the runtime delivers `_approval` on a resumed, approved node."""
    return build_books_operator().invoke(cap, {**inputs, **APPR})


BOOKS_GATED = {"books.record_revenue": {"subscription": "Pro plan", "amount": 250},
               "books.reverse_entry": {"entry": "ACC-JV-2026-00001"}}


@pytest.mark.parametrize("cap", sorted(BOOKS_GATED))
def test_books_gated_stage_without_approval_in_process_and_forged_http(client, cap):
    assert build_books_operator().invoke(cap, dict(BOOKS_GATED[cap]))["status"] == "pending_approval"
    for extra in ({}, APPR):
        res = client.post("/invoke", json={"capability": cap, "inputs": {**BOOKS_GATED[cap], **extra}}).json()["result"]
        assert res["status"] == "pending_approval" and res["executed"] is False
    assert _FakeClient.posts == [] and _FakeClient.puts == []        # ERPNext never written


def test_reverse_entry_only_claims_reversed_when_doc_transitions(monkeypatch):
    # non-2xx -> error, not reversed
    monkeypatch.setattr(_FakeClient, "put", lambda self, url, headers=None, json=None: _Resp(417, {}))
    res = _op("books.reverse_entry", {"entry": "X"})
    assert res["status"] == "error" and res["reversed"] is False
    # 2xx but the document did not become Cancelled (docstatus still 1) -> not reversed
    monkeypatch.setattr(_FakeClient, "put",
                        lambda self, url, headers=None, json=None: _Resp(200, {"data": {"docstatus": 1}}))
    res = _op("books.reverse_entry", {"entry": "X"})
    assert res["status"] == "error" and res["reversed"] is False
    # 2xx with no docstatus echoed, doc re-read shows Cancelled -> reversed
    monkeypatch.setattr(_FakeClient, "put", lambda self, url, headers=None, json=None: _Resp(200, {"data": {}}))
    monkeypatch.setattr(_FakeClient, "get", lambda self, url, headers=None, params=None: _Resp(200, {"data": {"docstatus": 2}}))
    assert _op("books.reverse_entry", {"entry": "X"})["reversed"] is True


def test_record_revenue_reports_error_when_submit_fails(monkeypatch):
    monkeypatch.setattr(_FakeClient, "put", lambda self, url, headers=None, json=None: _Resp(500, {}))
    res = _op("books.record_revenue", {"amount": 5})
    assert res["status"] == "error" and res["revenue_recorded"] is False


def _approved_categorize():
    return build_books_operator().invoke("books.categorize", {"_approval": {"approved": True}})


def test_categorize_reports_error_on_failed_put(monkeypatch):
    monkeypatch.setattr(_FakeClient, "put", lambda self, url, headers=None, json=None: _Resp(403, {}))
    res = _approved_categorize()
    assert res["status"] == "error" and res["result"]["erpnext_status"] == 403


def test_invoke_record_revenue_posts_journal_entry(client):
    res = _op("books.record_revenue", {"subscription": "Pro plan", "amount": 250})
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
    # ...and it is SUBMITTED (docstatus=1) so it actually posts to the GL — a draft records nothing.
    submit_url = "http://localhost:8092/api/resource/Journal%20Entry/ACC-JV-2026-00001"
    assert (submit_url, {"docstatus": 1}) in _FakeClient.put_bodies


def test_invoke_reverse_entry_cancels_journal_entry(client):
    res = _op("books.reverse_entry", {"entry": "ACC-JV-2026-00001"})
    assert res["status"] == "done" and res["action"] == "reverse_entry"
    assert res["reversed"] is True
    # the compensating action issued a cancel PUT against that Journal Entry.
    assert _FakeClient.puts == [
        "http://localhost:8092/api/resource/Journal%20Entry/ACC-JV-2026-00001"
    ]


def test_record_revenue_then_reverse_is_a_saga(client):
    """The onboarding mission's happy path then its compensating undo, end to end."""
    rec = _op("books.record_revenue", {"amount": 99})
    entry = rec["entry"]
    assert len(_FakeClient.posts) == 1 and entry

    rev = _op("books.reverse_entry", {"entry": entry})
    assert rev["reversed"] is True
    # record_revenue SUBMITTED the entry (docstatus=1, posts to GL) then reverse_entry CANCELLED it
    # (docstatus=2) — two PUTs to the same voucher, the full saga round-trip.
    url = f"http://localhost:8092/api/resource/Journal%20Entry/{entry}"
    assert _FakeClient.puts == [url, url]
    assert _FakeClient.put_bodies[0][1] == {"docstatus": 1}   # submit (record)
    assert _FakeClient.put_bodies[1][1] == {"docstatus": 2}   # cancel (reverse)


def test_categorize_without_approval_stages_preview_and_writes_nothing(client):
    res = client.post("/invoke", json={"capability": "books.categorize", "inputs": {"_approval": {"approved": True}}}
                      ).json()["result"]     # open HTTP strips forged approval markers
    assert res["status"] == "pending_approval" and res["categorize_executed"] is False
    assert res["mode"] == "tag_only"
    ch = res["proposed_changes"][0]
    assert ch["name"] == "ACC-BTN-001" and ch["set"]["reference_number"].startswith("CATEGORIZED:")
    assert _FakeClient.puts == []


def test_categorize_with_runtime_approval_writes_tag_only():
    res = _approved_categorize()
    assert res["status"] == "done" and res["action"] == "categorize" and res["mode"] == "tag_only"
    assert res["categorize_executed"] is True and res["result"]["transaction"] == "ACC-BTN-001"
    assert "no account allocation" in res["note"]
    assert _FakeClient.puts == ["http://localhost:8092/api/resource/Bank%20Transaction/ACC-BTN-001"]


def test_reconcile_matches_deposit_to_invoice(client):
    res = client.post("/invoke", json={"capability": "books.reconcile", "inputs": {}}).json()["result"]
    assert res["status"] == "done" and res["action"] == "reconcile"
    assert res["match"]["invoice"] == "ACC-SINV-001"
    assert res["match"]["deposit"] == "ACC-BTN-001"
    assert res["match"]["amount"] == "$4,500"
    assert _FakeClient.puts == []  # read-only: reconcile only stages the match


def test_close_refuses_to_stage_when_checks_fail(client):
    res = client.post("/invoke", json={"capability": "books.close", "inputs": {}}).json()["result"]
    assert res["status"] == "not_ready" and res["close_staged"] is False
    items = {c["item"] for c in res["failing_checks"]}
    assert {"Bank transactions categorized", "Bank transactions reconciled"} <= items
    assert _FakeClient.puts == [] and _FakeClient.posts == []


def test_close_checklist_is_computed_not_constant():
    d = core.fetch_activity(force=True)
    done = {c["item"]: c["done"] for c in d["checklist"]}
    assert done["Bank transactions categorized"] is False and done["No draft journal entries"] is True
    _FakeClient.drafts = {"Journal%20Entry": [{"name": "JV-DRAFT-1"}]}
    d = core.fetch_activity(force=True)
    done = {c["item"]: c["done"] for c in d["checklist"]}
    assert done["No draft journal entries"] is False
    assert {"item": "No draft journal entries", "count": 1} in d["blocking_checks"]


def test_close_stages_ready_when_all_checks_pass(monkeypatch):
    monkeypatch.setattr(core, "COMPANY", "Meridian Wealth Management")
    _RESOURCE_BAK = dict(_RESOURCE)
    _RESOURCE["Bank%20Transaction"] = []
    try:
        res = core.close({"closing_account": "Retained Earnings - MWM"})
    finally:
        _RESOURCE.update(_RESOURCE_BAK)
    assert res["status"] == "pending_approval" and res["close_staged"] is True


def test_close_not_ready_when_erpnext_unavailable(monkeypatch):
    monkeypatch.setattr(core, "erpnext_connected", lambda: False)
    res = core.close({"closing_account": "x"})
    assert res["status"] == "not_ready" and res["failing_checks"]


def test_idempotency_dedupes_side_effect(client):
    body = {"capability": "books.categorize", "inputs": {}, "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second
    assert _FakeClient.puts == []        # staged without approval: no write at all


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"books": "http://books"}, transport=_transport)
    rec = oc.invoke("books", "books.reconcile", {}, idempotency_key="m-1")
    assert rec["status"] == "done" and rec["match"]["invoice"] == "ACC-SINV-001"

    clo = oc.invoke("books", "books.close", {}, idempotency_key="m-2")
    assert clo["status"] == "not_ready" and clo["failing_checks"]


def test_close_executes_verifies_idempotent_and_rejects_inconsistent(monkeypatch):
    """N3+N4: books.close stages the PCV proposal without approval; on approval it SUBMITS a real ERPNext
    Period Closing Voucher, READS IT BACK to verify (submitted + company/FY + closing GL entry), is
    idempotent on retry, and REFUSES an inconsistent re-close of an already-closed period."""
    import json as _J
    state = {"pcv": None}

    class _PCVFake:
        posts: list = []
        puts: list = []

        def __enter__(self): return self
        def __exit__(self, *a): return False

        def get(self, url, headers=None, params=None):
            if "/api/resource/Account" in url:
                return _Resp(200, {"data": [{"name": "Retained Earnings - MWM"}]})
            if "/api/resource/Period%20Closing%20Voucher" in url:
                return _Resp(200, {"data": [state["pcv"]] if state["pcv"] else []})
            if "/api/resource/GL%20Entry" in url:
                return _Resp(200, {"data": [{"name": "GL-PCV-1"}] if state["pcv"] else []})
            return _Resp(200, {"data": []})

        def post(self, url, headers=None, json=None):
            _PCVFake.posts.append(url)
            return _Resp(200, {"data": {"name": "ACC-PCV-2026-001"}})

        def put(self, url, headers=None, json=None):
            _PCVFake.puts.append((url, json or {}))
            if (json or {}).get("docstatus") == 1 and url.endswith("ACC-PCV-2026-001"):
                state["pcv"] = {"name": "ACC-PCV-2026-001", "company": "Meridian Wealth Management",
                                "fiscal_year": "2026", "posting_date": "2026-06-30", "docstatus": 1}
            return _Resp(200, {"data": {}})

    _PCVFake.posts, _PCVFake.puts = [], []
    monkeypatch.setattr(core, "ERPNEXT_API_KEY", "test-key")
    monkeypatch.setattr(core, "httpx",
                        types.SimpleNamespace(get=_fake_get, Client=lambda timeout=None: _PCVFake()))
    core._CACHE.update(ts=0.0, data=None)

    base = {"company": "Meridian Wealth Management", "fiscal_year": "2026",
            "period_end": "2026-06-30", "closing_account": "Retained Earnings - MWM"}

    staged = core.close(dict(base))
    assert staged["status"] == "pending_approval" and not _PCVFake.posts           # nothing posted

    done = core.close({**base, "_approval": {"approved": True}})
    assert done["status"] == "done" and done["close_executed"] and done["verified"] is True
    assert done["voucher"] == "ACC-PCV-2026-001" and done["idempotent"] is False
    assert _PCVFake.posts and any(j.get("docstatus") == 1 for _, j in _PCVFake.puts)   # created + submitted

    n = len(_PCVFake.posts)
    again = core.close({**base, "_approval": {"approved": True}})
    assert again["status"] == "done" and again["idempotent"] is True and len(_PCVFake.posts) == n  # no 2nd post

    bad = core.close({**base, "period_end": "2026-12-31", "_approval": {"approved": True}})
    assert bad["status"] == "error" and "inconsistent" in (bad.get("error") or "")


def test_invoke_tenant_comes_from_the_token_into_record_revenue(monkeypatch):
    seen = []
    monkeypatch.setattr(core, "record_revenue", lambda body: seen.append(dict(body)) or {"status": "done"})
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", "a=tok-a,b=tok-b")
    app = FastAPI()
    app.include_router(build_books_operator().router())
    c = TestClient(app)
    call = lambda tok, **i: c.post("/invoke", headers={"Authorization": f"Bearer {tok}"}, json={
        "capability": "books.record_revenue", "inputs": {"amount": "5", **i}, "idempotency_key": "same"})
    assert call("tok-a", _tenant="b").status_code == 403 and seen == []
    assert call("tok-a").status_code == 200 and call("tok-b").status_code == 200
    assert [s["_tenant"] for s in seen] == ["a", "b"]
