"""Phase-1 "make it real" + auth-gated execution acceptance.

The live wiring (``live.py``) turns a real CrowdSec alert into a persisted, replayable SecurityCase; the
DEMO decision step is open and never touches the edge; the REAL kick-off is a separate step that only runs an
already-APPROVED action and is dry-run unless explicitly enabled. Drives the REAL Mission Runtime operator
with ``core`` patched offline (same approach as test_response.py); the live alert feed is injected so no test
touches a live CrowdSec LAPI.
"""
from __future__ import annotations

import importlib

import pytest

live = importlib.import_module("edge-sentinel.live")
evidence = importlib.import_module("edge-sentinel.evidence")
core = importlib.import_module("edge-sentinel.core")
operator_mod = importlib.import_module("edge-sentinel.operator")
CaseStore = importlib.import_module("edge-sentinel.case_store").CaseStore

ALERT = {
    "scenario": "crowdsecurity/ssh-bf",
    "source": {"value": "203.0.113.7", "scope": "Ip"},
    "created_at": "2026-10-01T10:00:00Z",
    "events_count": 12,
}


@pytest.fixture
def fake_crowdsec(monkeypatch):
    """Patch the sentinel core so the REAL operator runs against an in-memory CrowdSec (no live LAPI)."""
    bans: set[str] = set()
    monkeypatch.setattr(core, "block_ip", lambda inp: (bans.add(inp["ip"]) or {"id": f"cs-{inp['ip']}"}))
    monkeypatch.setattr(core, "unblock_ip", lambda inp: (bans.discard(inp["ip"]) or {"unblocked": inp["ip"]}))
    monkeypatch.setattr(core, "triage", lambda: {"active_bans": sorted(bans)})
    return bans


def _operator():
    return operator_mod.build_edge_sentinel_operator()


def _open(st):
    return live.case_summaries(st)[0]["open_action"]


def test_live_alert_becomes_replayable_case():
    st = CaseStore()
    ids = live.sync_cases(st, alerts_fn=lambda: [ALERT])
    assert len(ids) == 1
    ids2 = live.sync_cases(st, alerts_fn=lambda: [ALERT])
    assert ids2 == ids and len(st.cases) == 1    # idempotent

    s = live.case_summaries(st)[0]
    assert s["severity"] == "critical"
    assert s["status"] == "AWAITING_APPROVAL"
    assert s["open_action"]["state"] == "AWAITING_APPROVAL"
    assert s["open_action"]["capability"] == "sentinel.block_ip"
    assert s["open_action"]["parameters"]["ip"] == "203.0.113.7"

    detail = live.case_detail(ids[0], st)
    assert detail["evidence"] and detail["observations"] and detail["findings"]
    assert detail["actions"][0]["state"] == "AWAITING_APPROVAL"
    assert detail["replay"]["deterministic"] is True
    assert detail["replay"]["case_id"] == ids[0]


def test_sync_offline_is_empty_never_canned():
    st = CaseStore()

    def boom():
        raise RuntimeError("LAPI down")

    assert live.sync_cases(st, alerts_fn=boom) == []
    assert live.case_summaries(st) == []


def test_reject_never_touches_the_edge(fake_crowdsec):
    st = CaseStore()
    live.sync_cases(st, alerts_fn=lambda: [ALERT])
    cid = next(iter(st.cases))
    rid = _open(st)["request_id"]

    out = live.decide(cid, rid, approved=False, actor="demo", st=st)
    assert out["approved"] is False and out["awaiting_execution"] is False
    assert out["case_status"] == "INVESTIGATING"
    assert fake_crowdsec == set()


def test_approve_stages_but_does_not_touch_the_edge(fake_crowdsec):
    st = CaseStore()
    live.sync_cases(st, alerts_fn=lambda: [ALERT])
    cid = next(iter(st.cases))
    rid = _open(st)["request_id"]

    out = live.decide(cid, rid, approved=True, actor="demo", st=st)
    assert out["approved"] is True and out["awaiting_execution"] is True
    # action is APPROVED (awaiting the authenticated execute), NO receipt, edge untouched
    assert _open(st)["state"] == "APPROVED"
    assert st.receipts == {}
    assert fake_crowdsec == set()


def test_execute_requires_prior_approval(fake_crowdsec):
    st = CaseStore()
    live.sync_cases(st, alerts_fn=lambda: [ALERT])
    cid = next(iter(st.cases))
    rid = _open(st)["request_id"]
    with pytest.raises(live.ExecutionError):
        live.execute(cid, rid, actor="operator:alice", st=st, operator=_operator(), enabled=True)
    assert fake_crowdsec == set()


def test_execute_dry_run_default_does_not_touch_edge(fake_crowdsec):
    st = CaseStore()
    live.sync_cases(st, alerts_fn=lambda: [ALERT])
    cid = next(iter(st.cases))
    rid = _open(st)["request_id"]
    live.decide(cid, rid, approved=True, actor="demo", st=st)

    out = live.execute(cid, rid, actor="operator:alice", st=st, operator=_operator(), enabled=False)
    assert out["dry_run"] is True and out["executed"] is False
    assert out["receipt"]["status"] == "DRY_RUN"
    assert fake_crowdsec == set()                 # never touched the edge


def test_execute_enabled_runs_through_operator_and_verifies(fake_crowdsec):
    st = CaseStore()
    live.sync_cases(st, alerts_fn=lambda: [ALERT])
    cid = next(iter(st.cases))
    rid = _open(st)["request_id"]
    live.decide(cid, rid, approved=True, actor="demo", st=st)

    out = live.execute(cid, rid, actor="operator:alice", st=st, operator=_operator(), enabled=True)
    assert out["executed"] is True
    assert out["receipt"]["status"] == "SUCCEEDED"
    assert out["verification"]["verified"] is True
    assert out["case_status"] == "CONTAINED"
    assert "203.0.113.7" in fake_crowdsec
    assert live.case_summaries(st)[0]["open_action"] is None   # advanced past APPROVED


def test_decide_unknown_request_raises():
    st = CaseStore()
    live.sync_cases(st, alerts_fn=lambda: [ALERT])
    cid = next(iter(st.cases))
    with pytest.raises(KeyError):
        live.decide(cid, "act-doesnotexist", approved=True, actor="demo", st=st)
