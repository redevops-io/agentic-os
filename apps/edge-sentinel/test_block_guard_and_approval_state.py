"""Ban-request validation and the approval state machine (decide -> execute) for staged block actions."""
from __future__ import annotations

import importlib

import httpx
import pytest

live = importlib.import_module("edge-sentinel.live")
core = importlib.import_module("edge-sentinel.core")
ipguard = importlib.import_module("edge-sentinel.ipguard")
operator_mod = importlib.import_module("edge-sentinel.operator")
CaseStore = importlib.import_module("edge-sentinel.case_store").CaseStore

ALERT = {"scenario": "crowdsecurity/ssh-bf", "source": {"value": "203.0.113.7", "scope": "Ip"},
         "created_at": "2026-10-01T10:00:00Z", "events_count": 12}
ALERT_B = {"scenario": "crowdsecurity/ssh-bf", "source": {"value": "198.51.100.9", "scope": "Ip"},
           "created_at": "2026-10-01T11:00:00Z", "events_count": 9}


@pytest.fixture(autouse=True)
def _no_lapi(monkeypatch):
    monkeypatch.setattr(core, "CROWDSEC_LAPI_URL", "http://198.51.100.200:8080")
    monkeypatch.setattr(core, "CROWDSEC_FRONT_URL", "http://198.51.100.200:8080")
    monkeypatch.setattr(core, "fetch_activity", lambda force=False: {})
    monkeypatch.delenv("SENTINEL_PROTECTED_IPS", raising=False)


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "0.0.0.0", "::", "10.1.2.3", "172.16.5.5", "192.168.1.1",
                                "169.254.169.254", "fe80::1", "224.0.0.1", "ff02::1", "::ffff:10.0.0.1",
                                "::ffff:127.0.0.1", "100.64.1.1", "255.255.255.255"])
def test_block_ip_refuses_non_public_addresses(ip):
    out = core.block_ip({"ip": ip})
    assert out["status"] == "error" and out["ip_blocked"] is False
    assert out["error"].startswith("refused")


def test_block_ip_refuses_lapi_and_configured_addresses(monkeypatch):
    assert "service" in core.block_ip({"ip": "198.51.100.200"})["error"]
    monkeypatch.setenv("SENTINEL_PROTECTED_IPS", "192.0.2.50, ::ffff:192.0.2.51")
    assert "protected" in core.block_ip({"ip": "192.0.2.50"})["error"]
    assert "protected" in core.block_ip({"ip": "192.0.2.51"})["error"]


@pytest.mark.parametrize("dur", ["87600000h", "8d", "0h", "forever", {"h": 1}, 4, "4h; drop", ""])
def test_block_ip_rejects_bad_duration(dur):
    out = core.block_ip({"ip": "203.0.113.7", "duration": dur})
    assert out["status"] == "error" and "duration" in out["error"]


def test_duration_and_reason_normalised():
    assert ipguard.parse_duration(None) == ("4h", None)
    assert ipguard.parse_duration(" 1d2H ") == ("1d2h", None)
    assert len(ipguard.clean_reason("x" * 5000)) == ipguard.MAX_REASON_LEN
    assert "\n" not in ipguard.clean_reason("a\nb\x00c")
    assert ipguard.clean_reason({"a": 1}) == ipguard.DEFAULT_REASON


def test_block_ip_posts_capped_reason_for_public_ip(monkeypatch):
    sent = {}

    class Client:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            sent["body"] = json
            return httpx.Response(200)
        def get(self, url, headers=None, params=None):
            return httpx.Response(200, json=[{"type": "ban", "scope": "Ip", "value": params["ip"]}])

    monkeypatch.setattr(core.httpx, "Client", Client)
    out = core.block_ip({"ip": "203.0.113.7", "duration": "2h", "reason": "r" * 999})
    assert out["status"] == "done" and out["ip_blocked"] is True
    assert sent["body"][0]["duration"] == "2h" and len(sent["body"][0]["scenario"]) == 200


# ---- approval state machine ----

@pytest.fixture
def two_cases():
    st = CaseStore()
    live.sync_cases(st, alerts_fn=lambda: [ALERT, ALERT_B])
    rows = {r["title"]: r for r in live.case_summaries(st)}
    a = next(r for t, r in rows.items() if "203.0.113.7" in t)
    b = next(r for t, r in rows.items() if "198.51.100.9" in t)
    return st, a, b


@pytest.fixture
def bans(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "block_ip", lambda inp: (calls.append(inp["ip"]) or {"id": "x", "ip_blocked": True}))
    monkeypatch.setattr(core, "triage", lambda: {"active_bans": sorted(calls)})
    return calls


def _op():
    return operator_mod.build_edge_sentinel_operator()


def test_decide_rejects_request_from_another_case(two_cases):
    st, a, b = two_cases
    with pytest.raises(KeyError):
        live.decide(a["case_id"], b["open_action"]["request_id"], approved=True, st=st)
    with pytest.raises(KeyError):
        live.execute(a["case_id"], b["open_action"]["request_id"], actor="operator:x", st=st, enabled=False)


def test_decide_ignores_supplied_actor(two_cases):
    st, a, _ = two_cases
    out = live.decide(a["case_id"], a["open_action"]["request_id"], approved=True, actor="CEO", st=st)
    assert out["decision"]["actor"] == live.DEMO_ACTOR


def test_decide_only_from_awaiting_approval(two_cases, bans):
    st, a, _ = two_cases
    cid, rid = a["case_id"], a["open_action"]["request_id"]
    live.decide(cid, rid, approved=True, st=st)
    with pytest.raises(live.ExecutionError):
        live.decide(cid, rid, approved=True, st=st)          # already approved
    live.execute(cid, rid, actor="operator:x", st=st, operator=_op(), enabled=True)
    for flag in (True, False):
        with pytest.raises(live.ExecutionError):
            live.decide(cid, rid, approved=flag, st=st)       # terminal
    assert st.cases[cid].status.value == "CONTAINED"


def test_execute_once_then_terminal(two_cases, bans):
    st, a, _ = two_cases
    cid, rid = a["case_id"], a["open_action"]["request_id"]
    live.decide(cid, rid, approved=True, st=st)
    assert live.execute(cid, rid, actor="operator:x", st=st, operator=_op(), enabled=True)["executed"] is True
    with pytest.raises(live.ExecutionError):
        live.execute(cid, rid, actor="operator:x", st=st, operator=_op(), enabled=True)
    assert bans == ["203.0.113.7"]


def test_resync_does_not_rewind_executed_action(two_cases, bans):
    st, a, _ = two_cases
    cid, rid = a["case_id"], a["open_action"]["request_id"]
    live.decide(cid, rid, approved=True, st=st)
    live.execute(cid, rid, actor="operator:x", st=st, operator=_op(), enabled=True)
    live.sync_cases(st, alerts_fn=lambda: [ALERT, ALERT_B])
    assert st.actions[rid].state.value == "EXECUTED"
    assert st.cases[cid].status.value == "CONTAINED"
    with pytest.raises(live.ExecutionError):
        live.execute(cid, rid, actor="operator:x", st=st, operator=_op(), enabled=True)


def test_reject_is_terminal(two_cases, bans):
    st, a, _ = two_cases
    cid, rid = a["case_id"], a["open_action"]["request_id"]
    live.decide(cid, rid, approved=False, st=st)
    with pytest.raises(live.ExecutionError):
        live.decide(cid, rid, approved=True, st=st)
    with pytest.raises(live.ExecutionError):
        live.execute(cid, rid, actor="operator:x", st=st, operator=_op(), enabled=True)
    assert bans == []


def test_execute_uses_recorded_decision(two_cases, bans, monkeypatch):
    st, a, _ = two_cases
    cid, rid = a["case_id"], a["open_action"]["request_id"]
    out = live.decide(cid, rid, approved=True, st=st)
    seen = {}
    response = importlib.import_module("edge-sentinel.response")
    real = response.execute_response

    def spy(op, proposal, *, decision, **kw):
        seen["id"] = decision.decision_id
        return real(op, proposal, decision=decision, **kw)

    monkeypatch.setattr(response, "execute_response", spy)
    live.execute(cid, rid, actor="operator:x", st=st, operator=_op(), enabled=True)
    assert seen["id"] == out["decision"]["decision_id"]


def test_unblock_reports_transport_error(monkeypatch):
    class Boom:
        def __init__(self, *a, **k): ...
        def __enter__(self): raise httpx.ConnectError("down")
        def __exit__(self, *a): return False

    monkeypatch.setattr(core.httpx, "Client", Boom)
    out = core.unblock_ip({"ip": "203.0.113.7"})
    assert out["status"] == "error" and out["lifted"] is False
