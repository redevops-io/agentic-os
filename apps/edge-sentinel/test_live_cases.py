"""Phase-1 "make it real" acceptance: the live wiring (``live.py``) turns a real CrowdSec alert into a
persisted, replayable SecurityCase, and the governed response executes ONLY on approval.

Drives the REAL Mission Runtime operator with ``core`` patched offline (same approach as test_response.py),
so the governed path consumes the actual operator contract — never a replica. The live alert feed is injected
(``alerts_fn``) so no test touches a live CrowdSec LAPI.
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


def test_live_alert_becomes_replayable_case():
    st = CaseStore()
    ids = live.sync_cases(st, alerts_fn=lambda: [ALERT])
    assert len(ids) == 1

    # idempotent: re-syncing the same alert never duplicates the case
    ids2 = live.sync_cases(st, alerts_fn=lambda: [ALERT])
    assert ids2 == ids
    assert len(st.cases) == 1

    s = live.case_summaries(st)[0]
    assert s["severity"] == "critical"           # ssh-bf → critical
    assert s["status"] == "AWAITING_APPROVAL"    # critical finding proposes a (gated) block
    assert s["pending_action"]["capability"] == "sentinel.block_ip"
    assert s["pending_action"]["parameters"]["ip"] == "203.0.113.7"

    detail = live.case_detail(ids[0], st)
    assert detail["evidence"] and detail["observations"] and detail["findings"]
    assert detail["actions"][0]["state"] == "AWAITING_APPROVAL"
    # the replay invariant: the case id is reproducible from its immutable evidence
    assert detail["replay"]["deterministic"] is True
    assert detail["replay"]["case_id"] == ids[0]


def test_sync_offline_is_empty_never_canned():
    st = CaseStore()

    def boom():
        raise RuntimeError("LAPI down")

    assert live.sync_cases(st, alerts_fn=boom) == []
    assert live.case_summaries(st) == []         # empty, not a canned scenario


def test_reject_never_touches_the_edge(fake_crowdsec):
    st = CaseStore()
    live.sync_cases(st, alerts_fn=lambda: [ALERT])
    cid = next(iter(st.cases))
    rid = live.case_summaries(st)[0]["pending_action"]["request_id"]

    out = live.respond(cid, rid, approved=False, actor="soc", st=st, operator=_operator())
    assert out["approved"] is False
    assert out["receipt"] is None
    assert out["case_status"] == "INVESTIGATING"
    assert fake_crowdsec == set()                # the edge was never touched


def test_approve_executes_through_real_operator_and_verifies(fake_crowdsec):
    st = CaseStore()
    live.sync_cases(st, alerts_fn=lambda: [ALERT])
    cid = next(iter(st.cases))
    rid = live.case_summaries(st)[0]["pending_action"]["request_id"]

    out = live.respond(cid, rid, approved=True, actor="soc", st=st, operator=_operator())
    assert out["approved"] is True
    assert out["receipt"]["status"] == "SUCCEEDED"       # execution proof
    assert out["verification"]["verified"] is True       # distinct: the ban is actually present
    assert out["case_status"] == "CONTAINED"
    assert "203.0.113.7" in fake_crowdsec                # the real operator banned it

    # the pending action has advanced past approval — no longer awaiting
    assert live.case_summaries(st)[0]["pending_action"] is None


def test_approve_requires_the_pending_action(fake_crowdsec):
    st = CaseStore()
    live.sync_cases(st, alerts_fn=lambda: [ALERT])
    cid = next(iter(st.cases))
    with pytest.raises(KeyError):
        live.respond(cid, "act-doesnotexist", approved=True, actor="soc", st=st, operator=_operator())
