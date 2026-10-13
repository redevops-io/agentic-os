"""Route-level approval gating for agentic-books' OWN HTTP surface (/agent/run, /api/agent, console tools).

The gate lives in ``core``; a route gets an approval only by proving OPERATOR Basic auth (gate.py). These tests
drive the real FastAPI app with a fake ERPNext transport and assert, for EVERY /agent/run action, that
unauthenticated / wrong-credential / forged-body callers never cause a mutating HTTP call, while the operator
executes. ERPNext outages must come back as structured error dicts, never a 500. An AST test pins the
structure so a new route cannot bypass the gate.
"""
from __future__ import annotations

import ast
import base64
import types
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from books import core

HERE = Path(__file__).parent
OPERATOR = "ops:s3cret"
BANK_TXN = {"name": "ACC-BTN-001", "description": "Advisory fee | tag", "deposit": 4500, "withdrawal": 0,
            "status": "Unreconciled", "unallocated_amount": 4500, "date": "2026-06-20", "docstatus": 1}


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p, self.text = status, payload or {}, ""

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeErp:
    """A minimal ERPNext. ``txns``: bank transactions served (empty => the close checklist passes)."""

    def __init__(self, txns):
        self.calls: list[tuple[str, str]] = []
        self.txns = txns
        self.pcv = None

    @property
    def mutating(self):
        return [c for c in self.calls if c[0] in ("POST", "PUT", "PATCH", "DELETE")]

    def client(self, timeout=None):
        o = self

        class _C:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def get(s, url, headers=None, params=None):
                o.calls.append(("GET", url))
                if "/api/resource/Account" in url:
                    return _Resp(200, {"data": [{"name": "Retained Earnings - MWM", "root_type": "Equity"},
                                                {"name": "Sales - MWM", "root_type": "Income"},
                                                {"name": "Bank - MWM", "root_type": "Asset", "account_type": "Bank"}]})
                if "/api/resource/Bank%20Transaction" in url:
                    return _Resp(200, {"data": o.txns})
                if "/api/resource/Period%20Closing%20Voucher" in url:
                    return _Resp(200, {"data": [o.pcv] if o.pcv else []})
                if "/api/resource/GL%20Entry" in url:
                    return _Resp(200, {"data": [{"name": "GL-1"}] if o.pcv else []})
                if "/api/resource/Journal%20Entry/" in url:
                    return _Resp(200, {"data": {"docstatus": 2}})
                return _Resp(200, {"data": []})

            def post(s, url, headers=None, json=None):
                o.calls.append(("POST", url))
                return _Resp(200, {"data": {"name": "ACC-PCV-2026-001" if "Closing" in url else "ACC-JV-1"}})

            def put(s, url, headers=None, json=None):
                o.calls.append(("PUT", url))
                if (json or {}).get("docstatus") == 1 and url.endswith("ACC-PCV-2026-001"):
                    o.pcv = {"name": "ACC-PCV-2026-001", "company": core.COMPANY,
                             "fiscal_year": time_year(), "posting_date": "x", "docstatus": 1}
                return _Resp(200, {"data": {"docstatus": (json or {}).get("docstatus")}})
        return _C()


def time_year():
    import time
    return time.strftime("%Y")


@pytest.fixture()
def erp(monkeypatch):
    f = FakeErp([BANK_TXN])
    core._CACHE.update(ts=0.0, data=None)
    monkeypatch.setattr(core, "ERPNEXT_API_KEY", "k")
    monkeypatch.setattr(core, "CLOSING_ACCOUNT", "Retained Earnings - MWM")
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(
        get=lambda url, timeout=None, **kw: (f.calls.append(("GET", url)), _Resp(200, {"message": "Administrator"}))[1],
        Client=f.client, HTTPError=httpx.HTTPError))
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", OPERATOR)
    for e in ("OUTREACH_BASIC_AUTH", "OPERATOR_BASIC_AUTH_USER", "OPERATOR_BASIC_AUTH_PASS"):
        monkeypatch.delenv(e, raising=False)
    return f


@pytest.fixture()
def app_mod(load_app):
    return load_app("books")


@pytest.fixture()
def client(app_mod):
    return TestClient(app_mod.app)


def _basic(creds: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}


#: action -> (body, mutating calls expected when the operator runs it, txns to serve so the action is runnable)
PAYLOADS = {
    "categorize": ({}, 1, [BANK_TXN]),
    "reconcile": ({}, 0, [BANK_TXN]),                      # read-only: stages a match, writes nothing, ever
    "close": ({}, 2, []),                                  # POST voucher + PUT submit
    "record_revenue": ({"subscription": "Pro", "amount": 250}, 2, [BANK_TXN]),   # POST JE + PUT submit
    "reverse_entry": ({"entry": "ACC-JV-1"}, 1, [BANK_TXN]),                     # PUT cancel
}
READ_ONLY = {"reconcile"}
FORGERIES = [{"_approval": True}, {"_approval": {"approved": True, "by": "operator-basic"}}, {"_aPProval": True},
             {"__approval": 1}, {"_approval_x": 1}, {"a": {"b": {"_approval": True}}, "_approval": "yes"}]
BAD_AUTH = [{}, _basic("ops:wrong"), _basic("nobody:s3cret"), _basic(":"), {"Authorization": "Basic !!notb64"},
            {"Authorization": "Bearer ops:s3cret"}, {"Authorization": "Basic"}]


def _staged_or_ro(action, r):
    return action in READ_ONLY or r["status"] == "pending_approval"


def test_every_dispatch_action_has_a_gating_case(app_mod):
    assert set(app_mod.ACTIONS) == set(PAYLOADS), "add a route-gating case for every /agent/run action"


@pytest.mark.parametrize("action", sorted(PAYLOADS))
def test_unauthenticated_is_staged_and_nothing_mutates(action, client, erp):
    erp.txns = PAYLOADS[action][2]
    r = client.post("/agent/run", json={"action": action, **PAYLOADS[action][0]})
    assert r.status_code == 200 and _staged_or_ro(action, r.json()), r.text
    assert erp.mutating == []


@pytest.mark.parametrize("action", sorted(PAYLOADS))
@pytest.mark.parametrize("headers", BAD_AUTH)
def test_wrong_credentials_are_staged(action, headers, client, erp):
    erp.txns = PAYLOADS[action][2]
    r = client.post("/agent/run", json={"action": action, **PAYLOADS[action][0]}, headers=headers)
    assert _staged_or_ro(action, r.json()) and erp.mutating == []


@pytest.mark.parametrize("action", sorted(PAYLOADS))
def test_unset_operator_credential_fails_closed(action, client, erp, monkeypatch):
    erp.txns = PAYLOADS[action][2]
    monkeypatch.delenv("OPERATOR_BASIC_AUTH")
    for hdr in (_basic(":"), _basic("ops:s3cret"), {}):
        r = client.post("/agent/run", json={"action": action, **PAYLOADS[action][0]}, headers=hdr)
        assert _staged_or_ro(action, r.json())
    assert erp.mutating == []


@pytest.mark.parametrize("action", sorted(PAYLOADS))
@pytest.mark.parametrize("forged", FORGERIES)
def test_forged_approval_in_body_is_still_staged(action, forged, client, erp):
    erp.txns = PAYLOADS[action][2]
    for hdr in ({}, _basic("ops:wrong")):
        r = client.post("/agent/run", json={"action": action, **PAYLOADS[action][0], **forged}, headers=hdr)
        assert _staged_or_ro(action, r.json())
    assert erp.mutating == []


@pytest.mark.parametrize("action", sorted(PAYLOADS))
def test_operator_credentials_execute(action, client, erp):
    erp.txns = PAYLOADS[action][2]
    r = client.post("/agent/run", json={"action": action, **PAYLOADS[action][0]}, headers=_basic(OPERATOR))
    assert r.status_code == 200, r.text
    if action not in READ_ONLY:
        assert r.json()["status"] == "done", r.json()
    assert len(erp.mutating) == PAYLOADS[action][1], erp.mutating


def test_unknown_action_is_400(client, erp):
    r = client.post("/agent/run", json={"action": "drop_tables"}, headers=_basic(OPERATOR))
    assert r.status_code == 400 and erp.mutating == []


def test_console_tools_are_read_only_and_never_mutate(app_mod, client, erp):
    for msg in ("who owes us money", "close status", "record revenue 500", "reverse entry ACC-JV-1"):
        client.post("/api/agent", json={"message": msg, "_approval": True}, headers=_basic(OPERATOR))
    assert erp.mutating == []
    assert not any(t.side_effecting for t in app_mod._BOOKS_CONSOLE.tools)


# ── ERPNext unreachable: structured errors, never a 500, and the verifier fails the node cleanly ────────────
@pytest.fixture()
def down(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("erpnext down")

    class _Down:
        def __enter__(s):
            return s

        def __exit__(s, *a):
            return False
        get = post = put = delete = lambda s, *a, **k: boom()
    core._CACHE.update(ts=0.0, data=None)
    monkeypatch.setattr(core, "ERPNEXT_API_KEY", "k")
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(get=boom, Client=lambda timeout=None: _Down(),
                                                             HTTPError=httpx.HTTPError))


@pytest.mark.parametrize("action,flag", [("close", "close_staged"), ("record_revenue", "revenue_recorded")])
@pytest.mark.parametrize("auth", [{}, "op"])
def test_erpnext_outage_returns_structured_error_not_500(action, flag, auth, client, down, monkeypatch):
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", OPERATOR)
    headers = _basic(OPERATOR) if auth else {}
    r = client.post("/agent/run", json={"action": action, "amount": 5}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    if auth or action == "close":              # an unapproved record_revenue stages without touching ERPNext
        assert body["status"] in ("error", "not_ready") and body.get(flag) is False
        assert body.get("reason") or body.get("failing_checks") or body.get("error")


def test_outage_result_fails_the_value_aware_verifier(down):
    """The declared output is False, so a mission node reading it fails verification (not a crash)."""
    from agentic_os.mission.operator_sdk import Operator  # noqa: F401
    from books.operator import build_books_operator
    op = build_books_operator()
    res = op.invoke("books.close", {"_approval": {"approved": True}})
    assert res["status"] == "error" and res["close_staged"] is False and res["close_executed"] is False
    assert res["error"] == "erpnext unreachable" and res["reason"]
    rev = op.invoke("books.record_revenue", {"amount": 10, "_approval": {"approved": True}})
    assert rev["status"] == "error" and rev["revenue_recorded"] is False and rev["error"] == "erpnext unreachable"


# ── structure: no route (or console tool) can reach a side-effecting core function ungated ──────────────────
_MUTATORS = {"post", "put", "patch", "delete"}


def _funcs(tree):
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _calls_mutator(fn) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _MUTATORS
               for n in ast.walk(fn))


def test_core_mutators_check_approval_in_core():
    tree = ast.parse((HERE / "core.py").read_text())
    funcs = _funcs(tree)
    mutating = {n for n, f in funcs.items() if _calls_mutator(f)}
    assert {"categorize", "_execute_close", "record_revenue", "reverse_entry"} <= mutating
    for name in mutating:
        src = ast.unparse(funcs[name])
        if name.startswith("_execute"):           # only reachable from a caller that checked approval
            callers = [n for n, f in funcs.items() if n != name and any(
                isinstance(c, ast.Call) and getattr(c.func, "id", "") == name for c in ast.walk(f))]
            assert callers and all("approval" in ast.unparse(funcs[c]) for c in callers), (name, callers)
        else:
            assert "approval" in src, f"core.{name} mutates without an approval gate"


def test_routes_only_reach_core_through_the_gated_dispatch():
    tree = ast.parse((HERE / "app.py").read_text())
    funcs = _funcs(tree)
    routes = [f for f in funcs.values() if any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name)
        and d.func.value.id == "app" for d in f.decorator_list)]
    assert routes
    wrappers = {"_categorize", "_close", "_record_revenue", "_reverse_entry"}
    for r in routes:
        names = {n.id for n in ast.walk(r) if isinstance(n, ast.Name)}
        assert "core" not in names, f"route {r.name} calls core directly"
        assert not (names & wrappers), f"route {r.name} calls an action wrapper directly"
        if "ACTIONS" in names:
            assert "operator_approval" in names, f"route {r.name} dispatches without the server-side approval"
    # the marker is reachable from routes only: console tools and wrappers never mention it
    for name, f in funcs.items():
        if name.startswith("_t_") or name in wrappers:
            txt = ast.unparse(f)
            assert "operator_approval" not in txt and "operator-basic" not in txt and "OPERATOR_MARKER" not in txt
    # every wrapper that reaches core takes approval explicitly (default None)
    for w in wrappers:
        assert any(a.arg == "approval" for a in funcs[w].args.args)
        assert funcs[w].args.defaults, f"{w}: approval must default to None"
    # the console tools' only path to a side effect is a wrapper called WITHOUT approval
    for name, f in funcs.items():
        if name.startswith("_t_"):
            for c in ast.walk(f):
                if isinstance(c, ast.Call) and getattr(c.func, "id", "") in wrappers:
                    assert len(c.args) + len(c.keywords) <= 1 and not c.keywords, f"{name} passes an approval"
