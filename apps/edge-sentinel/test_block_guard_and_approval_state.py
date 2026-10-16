"""Ban-request validation and the persisted approval workflow (decide -> execute) for staged block actions."""
from __future__ import annotations

import importlib
import json
import os
import stat
import threading

import httpx
import pytest

live = importlib.import_module("edge-sentinel.live")
core = importlib.import_module("edge-sentinel.core")
ipguard = importlib.import_module("edge-sentinel.ipguard")
statefile = importlib.import_module("edge-sentinel.statefile")
operator_mod = importlib.import_module("edge-sentinel.operator")
CaseStore = importlib.import_module("edge-sentinel.case_store").CaseStore

ALERT_A = {"scenario": "crowdsecurity/ssh-bf", "source": {"value": "9.9.9.9", "scope": "Ip"},
           "created_at": "2026-10-01T10:00:00Z", "events_count": 12}
ALERT_B = {"scenario": "crowdsecurity/ssh-bf", "source": {"value": "1.1.1.1", "scope": "Ip"},
           "created_at": "2026-10-01T11:00:00Z", "events_count": 9}
LAPI_IP = "8.8.4.4"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("SENTINEL_PROTECTED_IPS", "SENTINEL_HOST_IPS", "SENTINEL_BLOCK_ALLOW_NETWORKS",
              "SENTINEL_BLOCK_ALLOW_WIDE_CIDR", "SENTINEL_BLOCK_MAX_HOURS", "SENTINEL_STATE_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(core, "CROWDSEC_LAPI_URL", f"http://{LAPI_IP}:8080")
    monkeypatch.setattr(core, "CROWDSEC_FRONT_URL", f"http://{LAPI_IP}:8080")
    monkeypatch.setattr(core, "fetch_activity", lambda force=False: {})
    monkeypatch.setattr(ipguard, "_default_gateway", lambda: "1.0.0.254")
    monkeypatch.setattr(ipguard, "_resolve", lambda host: [host] if host[:1].isdigit() else [])
    _Lapi.posts, _Lapi.deletes, _Lapi.readback_ok = [], [], True
    monkeypatch.setattr(core.httpx, "Client", _Lapi)      # no test ever reaches a real network
    live.reset_store()


# ---- target validation ----

@pytest.mark.parametrize("ip", [
    "0.0.0.0", "0.0.0.1", "10.1.2.3", "100.64.1.1", "127.0.0.1", "169.254.169.254", "172.16.5.5", "172.31.255.255",
    "192.0.0.8", "192.0.2.10", "192.88.99.1", "192.168.1.1", "198.18.0.1", "198.19.255.255", "198.51.100.7",
    "203.0.113.7", "224.0.0.1", "239.1.1.1", "240.0.0.1", "255.255.255.255",
    "::", "::1", "fe80::1", "fec0::1", "fc00::1", "fd12::5", "ff02::1", "2001:db8::1", "2001:0:4136:e378::1",
    "100::1", "3fff::1", "3fff:fff::1"])
def test_non_public_targets_are_refused(ip):
    with pytest.raises(ipguard.BlockRefused):
        ipguard.check_target(ip)
    out = core.block_ip({"ip": ip})
    assert out["status"] == "error" and out["ip_blocked"] is False and "refused" in out["error"]


@pytest.mark.parametrize("ip", ["::ffff:127.0.0.1", "::ffff:7f00:1", "::ffff:10.0.0.1", "64:ff9b::a00:1",
                                "2002:7f00:1::", "2002:c0a8:101::1"])
def test_embedded_ipv4_is_judged_by_the_address_it_carries(ip):
    with pytest.raises(ipguard.BlockRefused):
        ipguard.check_target(ip)


@pytest.mark.parametrize("ip", ["9.9.9.9", "1.1.1.1", "8.8.8.8", "2606:4700:4700::1111", "2620:fe::fe"])
def test_public_targets_are_accepted(ip):
    assert ipguard.check_target(ip).value == ip


@pytest.mark.parametrize("raw", ["", "  ", "01.1.1.1", "1.1.1", "1.1.1.1.1", "256.1.1.1", "0x08.8.8.8", "134744072",
                                 "fe80::1%eth0", "2606:4700:4700:0:0:0:0:1111",
                                 "2606:4700:4700::1111%1", "１.１.１.１", "1.1.1.1/33", "1.1.1.1/", "9.9.9.9/24",
                                 "abc", None, 5, ["1.1.1.1"], {"ip": 1}])
def test_malformed_or_non_canonical_targets_are_refused(raw):
    with pytest.raises(ipguard.BlockRefused):
        ipguard.check_target(raw)


def test_ranges_follow_width_limit_and_allow_list(monkeypatch):
    assert ipguard.check_target("9.9.9.0/24") == ipguard.BlockTarget("9.9.9.0/24", "Range")
    for wide in ("9.9.0.0/16", "2606:4700::/48"):
        with pytest.raises(ipguard.BlockRefused) as e:
            ipguard.check_target(wide)
        assert e.value.code == "too_wide"
    monkeypatch.setenv("SENTINEL_BLOCK_ALLOW_WIDE_CIDR", "1")
    assert ipguard.check_target("9.9.0.0/16").scope == "Range"
    with pytest.raises(ipguard.BlockRefused):       # still overlaps never-bannable space
        ipguard.check_target("0.0.0.0/0")


def test_private_space_only_inside_allow_list(monkeypatch):
    monkeypatch.setenv("SENTINEL_BLOCK_ALLOW_NETWORKS", "10.99.0.0/16, fd12::/32")
    assert ipguard.check_target("10.99.1.2").value == "10.99.1.2"
    assert ipguard.check_target("fd12::7").value == "fd12::7"
    with pytest.raises(ipguard.BlockRefused):
        ipguard.check_target("10.98.1.2")
    with pytest.raises(ipguard.BlockRefused):       # the allow-list never opens loopback
        monkeypatch.setenv("SENTINEL_BLOCK_ALLOW_NETWORKS", "127.0.0.0/8")
        ipguard.check_target("127.0.0.1")


def test_protected_hosts_are_refused(monkeypatch):
    kw = dict(service_urls=(core.CROWDSEC_LAPI_URL,))
    for ip in (LAPI_IP, "1.0.0.254"):               # LAPI host, default gateway
        with pytest.raises(ipguard.BlockRefused) as e:
            ipguard.check_target(ip, **kw)
        assert e.value.code == "protected"
    monkeypatch.setenv("SENTINEL_PROTECTED_IPS", "9.9.9.9")
    monkeypatch.setenv("SENTINEL_HOST_IPS", "1.1.1.0/28")
    for ip in ("9.9.9.9", "1.1.1.5"):
        with pytest.raises(ipguard.BlockRefused):
            ipguard.check_target(ip)
    assert core.block_ip({"ip": LAPI_IP})["refused"] is True
    with pytest.raises(ipguard.BlockRefused):       # a range that contains a protected address
        ipguard.check_target("1.1.1.0/24")


def test_operator_address_is_refused():
    with pytest.raises(ipguard.BlockRefused):
        ipguard.check_target("8.8.8.8", operator_ip="8.8.8.8")
    tok = ipguard.OPERATOR_IP.set("8.8.8.8")
    try:
        with pytest.raises(ipguard.BlockRefused):
            ipguard.check_target("8.8.8.8")
    finally:
        ipguard.OPERATOR_IP.reset(tok)
    assert ipguard.check_target("8.8.8.8").value == "8.8.8.8"


@pytest.mark.parametrize("dur", ["87600000h", "8d", "169h", "0h", "forever", {"h": 1}, 4, "4h; drop", "٤h",
                                 "1h1h1h1h", "99999h"])
def test_bad_durations_are_refused(dur):
    out = core.block_ip({"ip": "9.9.9.9", "duration": dur})
    assert out["status"] == "error" and "duration" in out["error"]


def test_duration_cap_and_reason_normalisation(monkeypatch):
    assert ipguard.parse_duration(None) == ("4h", None)
    assert ipguard.parse_duration(" 1D2H ") == ("1d2h", None)
    assert ipguard.parse_duration("168h")[1] is None and ipguard.parse_duration("7d")[1] is None
    monkeypatch.setenv("SENTINEL_BLOCK_MAX_HOURS", "2")
    assert ipguard.parse_duration("3h")[1] is not None
    assert len(ipguard.clean_reason("x" * 5000)) == ipguard.MAX_REASON_LEN
    assert "\n" not in ipguard.clean_reason("a\nb\x00c")
    assert ipguard.clean_reason({"a": 1}) == ipguard.DEFAULT_REASON


class _Lapi:
    """Fake httpx.Client that records writes and answers the read-back."""
    posts: list = []
    deletes: list = []
    readback_ok = True

    def __init__(self, *a, **k): ...
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def post(self, url, headers=None, json=None):
        _Lapi.posts.append(json)
        return httpx.Response(200)

    def delete(self, url, headers=None, params=None):
        _Lapi.deletes.append(params)
        return httpx.Response(200)

    def get(self, url, headers=None, params=None):
        if not _Lapi.readback_ok:
            raise httpx.ConnectError("down")
        rows = [{"type": "ban", "scope": "Ip", "value": params["ip"]} for p in _Lapi.posts
                if "ip" in params and p[0]["value"] == params["ip"]]
        return httpx.Response(200, json=rows)


@pytest.fixture
def lapi():
    return _Lapi


def test_block_ip_posts_normalised_request(lapi):
    out = core.block_ip({"ip": "9.9.9.9", "duration": "2h", "reason": "r" * 999})
    assert out["status"] == "done" and out["ip_blocked"] is True
    sent = lapi.posts[0][0]
    assert sent["duration"] == "2h" and sent["scope"] == "Ip" and len(sent["scenario"]) == 200


def test_unblock_reports_unknown_when_readback_fails(lapi):
    core.block_ip({"ip": "9.9.9.9"})
    lapi.posts.clear()
    lapi.readback_ok = False
    out = core.unblock_ip({"ip": "9.9.9.9"})
    assert out["status"] == "error" and out["lifted"] is None


def test_unblock_success_and_transport_error(lapi, monkeypatch):
    assert core.unblock_ip({"ip": "9.9.9.9"})["lifted"] is True

    class Boom:
        def __init__(self, *a, **k): ...
        def __enter__(self): raise httpx.ConnectError("down")
        def __exit__(self, *a): return False

    monkeypatch.setattr(core.httpx, "Client", Boom)
    out = core.unblock_ip({"ip": "9.9.9.9"})
    assert out["status"] == "error" and out["lifted"] is False


# ---- approval workflow ----

@pytest.fixture
def two_cases():
    st = CaseStore()
    live.sync_cases(st, alerts_fn=lambda: [ALERT_A, ALERT_B])
    rows = {r["title"]: r for r in live.case_summaries(st)}
    a = next(r for t, r in rows.items() if "9.9.9.9" in t)
    b = next(r for t, r in rows.items() if "1.1.1.1" in t)
    return st, a, b


@pytest.fixture
def bans(monkeypatch):
    calls: list = []
    monkeypatch.setattr(core, "block_ip", lambda inp: (calls.append(inp["ip"]) or {"id": "x", "ip_blocked": True}))
    monkeypatch.setattr(core, "triage", lambda: {"active_bans": sorted(calls)})
    return calls


def _op():
    return operator_mod.build_edge_sentinel_operator()


def _ids(row):
    return row["case_id"], row["open_action"]["request_id"]


def _run(st, row):
    cid, rid = _ids(row)
    return live.execute(cid, rid, actor="operator:x", st=st, operator=_op(), enabled=True)


def test_request_must_belong_to_the_case(two_cases):
    st, a, b = two_cases
    with pytest.raises(KeyError):
        live.decide(a["case_id"], b["open_action"]["request_id"], approved=True, actor="operator:x", st=st)
    with pytest.raises(KeyError):
        live.execute(a["case_id"], b["open_action"]["request_id"], actor="operator:x", st=st, enabled=False)
    with pytest.raises(KeyError):
        live.preview_decide(a["case_id"], b["open_action"]["request_id"], approved=True, st=st)


def test_preview_records_nothing(two_cases):
    st, a, _ = two_cases
    cid, rid = _ids(a)
    for flag in (True, False):
        out = live.preview_decide(cid, rid, approved=flag, st=st)
        assert out["simulated"] is True and out["awaiting_execution"] is False
    assert st.actions[rid].state.value == "AWAITING_APPROVAL" and not st.decisions
    live.decide(cid, rid, approved=True, actor="operator:x", st=st)
    with pytest.raises(live.ExecutionError):
        live.preview_decide(cid, rid, approved=True, st=st)


def test_decision_records_given_actor(two_cases):
    st, a, _ = two_cases
    cid, rid = _ids(a)
    out = live.decide(cid, rid, approved=True, actor="operator:alice", st=st)
    assert out["decision"]["actor"] == "operator:alice"


def test_decide_only_from_awaiting_approval(two_cases, bans):
    st, a, _ = two_cases
    cid, rid = _ids(a)
    live.decide(cid, rid, approved=True, actor="operator:x", st=st)
    with pytest.raises(live.ExecutionError):
        live.decide(cid, rid, approved=True, actor="operator:x", st=st)
    _run(st, a)
    for flag in (True, False):
        with pytest.raises(live.ExecutionError):
            live.decide(cid, rid, approved=flag, actor="operator:x", st=st)
    assert st.cases[cid].status.value == "CONTAINED"


def test_withdraw_then_final(two_cases, bans):
    st, a, _ = two_cases
    cid, rid = _ids(a)
    live.decide(cid, rid, approved=True, actor="operator:x", st=st)
    live.decide(cid, rid, approved=False, actor="operator:x", st=st)
    for fn in (lambda: live.decide(cid, rid, approved=True, actor="operator:x", st=st),
               lambda: live.execute(cid, rid, actor="operator:x", st=st, operator=_op(), enabled=True)):
        with pytest.raises(live.ExecutionError):
            fn()
    assert bans == []


def test_execute_once_then_final(two_cases, bans):
    st, a, _ = two_cases
    cid, rid = _ids(a)
    live.decide(cid, rid, approved=True, actor="operator:x", st=st)
    assert _run(st, a)["executed"] is True
    with pytest.raises(live.ExecutionError):
        _run(st, a)
    assert bans == ["9.9.9.9"]


def test_concurrent_executes_ban_once(two_cases, monkeypatch):
    st, a, _ = two_cases
    cid, rid = _ids(a)
    live.decide(cid, rid, approved=True, actor="operator:x", st=st)
    calls: list = []
    gate = threading.Event()

    def slow(inp):
        calls.append(inp["ip"])
        gate.wait(2)
        return {"id": "x", "ip_blocked": True}

    monkeypatch.setattr(core, "block_ip", slow)
    monkeypatch.setattr(core, "triage", lambda: {"active_bans": list(calls)})
    results: list = []

    def go():
        try:
            results.append(live.execute(cid, rid, actor="operator:x", st=st, operator=_op(), enabled=True))
        except live.ExecutionError as e:
            results.append(e)

    t1 = threading.Thread(target=go)
    t1.start()
    while not calls:
        pass
    t2 = threading.Thread(target=go)
    t2.start()
    t2.join(3)
    with pytest.raises(live.ExecutionError):        # a decision cannot change an action that is executing
        live.decide(cid, rid, approved=False, actor="operator:x", st=st)
    gate.set()
    t1.join(5)
    assert calls == ["9.9.9.9"]
    assert sum(isinstance(r, dict) for r in results) == 1 and sum(isinstance(r, Exception) for r in results) == 1


def test_resync_does_not_rewind_executed_action(two_cases, bans):
    st, a, _ = two_cases
    cid, rid = _ids(a)
    live.decide(cid, rid, approved=True, actor="operator:x", st=st)
    _run(st, a)
    live.sync_cases(st, alerts_fn=lambda: [ALERT_A, ALERT_B])
    assert st.actions[rid].state.value == "EXECUTED" and st.cases[cid].status.value == "CONTAINED"
    with pytest.raises(live.ExecutionError):
        _run(st, a)


def test_execute_uses_recorded_decision(two_cases, bans, monkeypatch):
    st, a, _ = two_cases
    cid, rid = _ids(a)
    out = live.decide(cid, rid, approved=True, actor="operator:x", st=st)
    response = importlib.import_module("edge-sentinel.response")
    real, seen = response.execute_response, {}

    def spy(op, proposal, *, decision, **kw):
        seen["id"], seen["actor"] = decision.decision_id, decision.actor
        return real(op, proposal, decision=decision, **kw)

    monkeypatch.setattr(response, "execute_response", spy)
    _run(st, a)
    assert seen == {"id": out["decision"]["decision_id"], "actor": "operator:x"}


def test_failed_execution_retries_with_backoff_then_needs_reapproval(two_cases, monkeypatch):
    st, a, _ = two_cases
    cid, rid = _ids(a)
    clock = [1000.0]
    monkeypatch.setattr(live, "_NOW", lambda: clock[0])
    monkeypatch.setattr(core, "block_ip", lambda inp: {"status": "error", "error": "lapi down"})
    monkeypatch.setattr(core, "triage", lambda: {})
    live.decide(cid, rid, approved=True, actor="operator:x", st=st)
    out = _run(st, a)
    assert out["executed"] is False and st.actions[rid].state.value == "FAILED"
    with pytest.raises(live.ExecutionError) as e:
        _run(st, a)
    assert "retry in" in str(e.value)
    for _ in range(live.MAX_ATTEMPTS - 1):
        clock[0] += 400
        assert _run(st, a)["executed"] is False
    clock[0] += 400
    with pytest.raises(live.ExecutionError) as e:
        _run(st, a)
    assert "re-approval" in str(e.value)
    live.decide(cid, rid, approved=True, actor="operator:x", st=st)       # exhausted -> operator re-approve
    assert st.actions[rid].state.value == "APPROVED"
    monkeypatch.setattr(core, "block_ip", lambda inp: {"id": "x", "ip_blocked": True})
    assert _run(st, a)["executed"] is True


# ---- persistence ----

def test_state_survives_restart_and_is_private(two_cases, bans, monkeypatch, tmp_path):
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path / "state"))
    st, a, b = two_cases
    cid, rid = _ids(a)
    live.decide(cid, rid, approved=True, actor="operator:x", st=st)
    _run(st, a)
    live.decide(*_ids(b), approved=False, actor="operator:x", st=st)
    f = tmp_path / "state" / statefile.FILENAME
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert not [p for p in os.listdir(tmp_path / "state") if p.endswith(".tmp")]

    live.reset_store()
    st2 = CaseStore()
    live.sync_cases(st2, alerts_fn=lambda: [ALERT_A, ALERT_B])             # a "restart": fresh ingest + overlay
    assert st2.actions[rid].state.value == "EXECUTED" and st2.cases[cid].status.value == "CONTAINED"
    assert st2.actions[_ids(b)[1]].state.value == "REJECTED"
    with pytest.raises(live.ExecutionError):
        live.execute(cid, rid, actor="operator:x", st=st2, operator=_op(), enabled=True)
    with pytest.raises(live.ExecutionError):
        live.decide(*_ids(b), approved=True, actor="operator:x", st=st2)


def test_corrupt_state_fails_closed(two_cases, bans, monkeypatch, tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    (d / statefile.FILENAME).write_text("{not json")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(d))
    st, a, _ = two_cases
    cid, rid = _ids(a)
    with pytest.raises(live.ExecutionError):
        live.decide(cid, rid, approved=True, actor="operator:x", st=st)
    assert live.state_health()["ok"] is False
    (d / statefile.FILENAME).write_text(json.dumps({"version": 2}))
    with pytest.raises(statefile.StateUnavailable):
        statefile.load()


def test_unwritable_state_prevents_execution(two_cases, bans, monkeypatch, tmp_path):
    st, a, _ = two_cases
    cid, rid = _ids(a)
    live.decide(cid, rid, approved=True, actor="operator:x", st=st)
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path / "state"))

    def boom(data):
        raise statefile.StateUnavailable("disk full")

    monkeypatch.setattr(statefile, "save", boom)
    with pytest.raises(live.ExecutionError):
        _run(st, a)
    assert bans == [] and st.actions[rid].state.value == "APPROVED"


# ---- HTTP surface ----

def test_respond_endpoint_preview_vs_operator(two_cases, monkeypatch):
    import base64
    from fastapi.testclient import TestClient
    appmod = importlib.import_module("edge-sentinel.app")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_USER", "op")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_PASS", "pw")
    st, a, _ = two_cases
    monkeypatch.setattr(live, "_STORE", st)
    cid, rid = _ids(a)
    c = TestClient(appmod.app)
    r = c.post(f"/api/cases/{cid}/respond", json={"request_id": rid, "approved": False, "actor": "CEO"})
    assert r.status_code == 200 and r.json()["simulated"] is True
    assert st.actions[rid].state.value == "AWAITING_APPROVAL" and not st.decisions
    hdr = {"Authorization": "Basic " + base64.b64encode(b"op:pw").decode()}
    r = c.post(f"/api/cases/{cid}/respond", json={"request_id": rid, "approved": True, "actor": "CEO"}, headers=hdr)
    assert r.status_code == 200 and r.json()["decision"]["actor"] == "operator:op"
    r = c.post(f"/api/cases/{cid}/respond", json={"request_id": rid, "approved": True}, headers=hdr)
    assert r.status_code == 409
