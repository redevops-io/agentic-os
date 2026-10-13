"""billing closed loop is OPERABLE: parked missions survive a restart, outcomes are observed on a schedule/endpoint,
and the policy refits only through the promotion gate.

A "restart" is simulated by building NEW process objects (runtime, stores, launcher, planner) over the same state dir.
"""
from __future__ import annotations

import json
import os
import types

import pytest
from fastapi.testclient import TestClient

from agentic_os.mission.types import MissionState
from agentic_os.policy_promotion import STATIC_POLICY

from billing import core, decision, loop_api
from billing.test_closed_loop import AUTH, DAY, NOW, FakeLago, _decide


@pytest.fixture()
def lago(monkeypatch):
    fake = FakeLago()
    core._CACHE.update(ts=0.0, data=None)
    monkeypatch.setattr(core, "LAGO_API_KEY", "k")
    monkeypatch.setattr(core, "VERIFY_ATTEMPTS", 1)
    monkeypatch.setattr(core, "_sleep", lambda s: None)
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(get=fake.get, Client=fake.client))
    return fake


def _boot(state_dir, clock=None):
    clock = clock or {"now": NOW}
    return decision.build_loop(str(state_dir), clock=lambda: clock["now"]), clock


def test_restart_can_approve_a_parked_mission(lago, tmp_path):
    lp1, _ = _boot(tmp_path)
    cyc = _decide(lp1)
    assert cyc.mission_state == "waiting_human" and lago.retries == []
    assert (tmp_path / "missions.jsonl").exists()

    lp2, _ = _boot(tmp_path)                                         # <- the restart: nothing in memory survives
    assert lp2.runtime is not lp1.runtime
    assert lp2.resumed["resumed"] == [cyc.mission_id] and lp2.resumed["skipped"] == {}
    assert lp2.launched[cyc.mission_id]["inputs"] == {"account_id": "acme", "invoice_ids": ["a1", "a2"]}
    out = decision.approve_mission(lp2, cyc.mission_id)
    assert out["state"] == "succeeded" and sorted(lago.retries) == ["a1", "a2"]
    assert out["dunning"]["dunning_verified"] is True and out["dunning"]["settled_count"] == 2
    rec = decision.latest_records(lp2.interventions)[0]
    assert rec.executed_at == NOW and rec.approved_at == NOW and rec.execution_ref == cyc.mission_id

    lp3, _ = _boot(tmp_path)                                         # a second restart: nothing left to resume
    assert lp3.resumed["resumed"] == []
    n = len(lago.retries)
    again = decision.approve_mission(lp3, cyc.mission_id) if cyc.mission_id in lp3.launched else None
    assert again is None and len(lago.retries) == n                  # an executed mission is never re-run


def test_restart_then_reject_executes_nothing(lago, tmp_path):
    lp1, _ = _boot(tmp_path)
    cyc = _decide(lp1)
    lp2, _ = _boot(tmp_path)
    out = decision.approve_mission(lp2, cyc.mission_id, decision="reject")
    assert lago.retries == [] and out["state"] != "succeeded"
    assert decision.latest_records(lp2.interventions)[0].executed_at is None
    lp3, _ = _boot(tmp_path)
    assert lp3.resumed["resumed"] == []                              # rejected => terminal, not parked any more


def test_http_approve_works_after_restart(lago, tmp_path, monkeypatch, load_app):
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "op:secret")
    lp1, _ = _boot(tmp_path)
    mid = _decide(lp1).mission_id
    lp2, _ = _boot(tmp_path)
    loop_api.set_loop(lp2)
    try:
        r = TestClient(load_app("billing").app).post(f"/api/decide/{mid}/approve", headers=AUTH, json={})
    finally:
        loop_api.set_loop(None)
    assert r.status_code == 200 and r.json()["state"] == "succeeded" and sorted(lago.retries) == ["a1", "a2"]


def test_resume_refuses_a_mission_whose_scope_no_longer_matches_the_ledger_digest(lago, tmp_path):
    lp1, _ = _boot(tmp_path)
    mid = _decide(lp1).mission_id
    path = tmp_path / "interventions.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    for r in rows:                                                   # tamper: widen the scope with another invoice
        r["evidence_refs"] = list(r["evidence_refs"]) + ["invoice:b1"]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    lp2, _ = _boot(tmp_path)
    assert lp2.resumed["resumed"] == [] and mid in lp2.resumed["skipped"]
    assert "inputs" in lp2.resumed["skipped"][mid].lower()
    with pytest.raises(KeyError):
        decision.approve_mission(lp2, mid)
    assert lago.retries == []


def test_in_memory_loop_is_unchanged_without_a_state_dir(lago, monkeypatch):
    monkeypatch.delenv("BILLING_STATE_DIR", raising=False)
    lp = decision.build_loop()
    assert lp.state_dir == "" and lp.resumed == {}


# ── outcomes: observed after N days via an injected clock, learner updated only through the gate ─────────────
def test_outcome_observation_after_n_days_and_refit_only_through_the_gate(lago, tmp_path):
    lp, clock = _boot(tmp_path)
    decision.approve_mission(lp, _decide(lp).mission_id)
    clock["now"] = NOW + 2 * DAY
    assert decision.observe_outcomes(interventions=lp.interventions, outcomes=lp.outcomes, ingestor=lp.ingestor,
                                     now=clock["now"]) == []         # too early: nothing observed
    assert lp.outcomes.load() == []
    clock["now"] = NOW + 8 * DAY
    rows = loop_api.run_observe(lp)                                  # the SAME path the endpoint + scheduler use
    assert len(rows) == 1 and rows[0]["outcome"] == "payment_collected"
    assert len(lp.outcomes.load()) == 1
    assert loop_api.run_observe(lp) == []                            # idempotent
    # the learner moves only through the promotion gate: too little data => refused, active policy untouched
    rep = loop_api.run_refit(lp, apply=True)
    assert rep["promoted"] is False and rep["reasons"] and lp.policies.active is STATIC_POLICY
    lp2, _ = _boot(tmp_path)                                         # and a restart still sees the outcome
    assert len(lp2.outcomes.load()) == 1 and lp2.policies.active.version == STATIC_POLICY.version


def test_refit_never_bypasses_the_gate(lago, tmp_path, monkeypatch):
    lp, _ = _boot(tmp_path)
    calls = []
    real = decision.refit_policy
    monkeypatch.setattr(decision, "refit_policy", lambda *a, **k: (calls.append(k), real(*a, **k))[1])
    monkeypatch.setattr(lp.policies, "promote", lambda *a, **k: pytest.fail("promote called outside the gate"))
    loop_api.run_refit(lp, apply=True)
    assert calls and calls[0].get("apply") is True


# ── HTTP endpoints (operator auth, fail closed) ──────────────────────────────────────────────────────────────
@pytest.fixture()
def http(lago, tmp_path, monkeypatch, load_app):
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "op:secret")
    lp, clock = _boot(tmp_path)
    loop_api.set_loop(lp)
    yield TestClient(load_app("billing").app), lp, clock
    loop_api.set_loop(None)


@pytest.mark.parametrize("path", ["/api/outcomes/observe", "/api/policy/refit"])
def test_endpoints_fail_closed(path, http, monkeypatch):
    c, lp, _ = http
    assert c.post(path, json={}).status_code == 401
    assert c.post(path, json={}, headers={"Authorization": "Basic b3A6YmFk"}).status_code == 401
    monkeypatch.delenv("OPERATOR_BASIC_AUTH")
    monkeypatch.delenv("OUTREACH_BASIC_AUTH", raising=False)
    assert c.post(path, json={}, headers=AUTH).status_code == 503


def test_http_observe_and_refit(http, lago):
    c, lp, clock = http
    decision.approve_mission(lp, _decide(lp).mission_id)
    clock["now"] = NOW + 8 * DAY
    r = c.post("/api/outcomes/observe", headers=AUTH, json={"_approval": True})
    assert r.status_code == 200 and r.json()["observed"][0]["outcome"] == "payment_collected"
    f = c.post("/api/policy/refit", headers=AUTH, json={"apply": True, "_approval": True}).json()
    assert f["promoted"] is False and any("insufficient held-out data" in x for x in f["reasons"])
    assert f["active_policy"] == STATIC_POLICY.version


# ── optional background scheduler (default off, single-flight lock) ─────────────────────────────────────────
def test_scheduler_is_off_by_default_and_runs_one_tick_under_a_lock(lago, tmp_path, monkeypatch):
    monkeypatch.delenv("BILLING_OUTCOME_INTERVAL_S", raising=False)
    assert loop_api.scheduler_interval() == 0.0 and loop_api.start_scheduler(None) is None
    monkeypatch.setenv("BILLING_OUTCOME_INTERVAL_S", "nonsense")
    assert loop_api.scheduler_interval() == 0.0
    monkeypatch.setenv("BILLING_OUTCOME_INTERVAL_S", "60")
    assert loop_api.scheduler_interval() == 60.0
    lp, clock = _boot(tmp_path)
    decision.approve_mission(lp, _decide(lp).mission_id)
    clock["now"] = NOW + 8 * DAY
    assert loop_api.scheduler_tick(lp)["observed"] == 1
    assert loop_api.scheduler_tick(lp)["observed"] == 0
    assert lp.lock.acquire(blocking=False)
    try:                                                              # a tick while another run holds the lock skips
        import threading
        out = {}
        t = threading.Thread(target=lambda: out.update(loop_api.scheduler_tick(lp)))
        t.start(); t.join(5)
        assert out.get("skipped") == "busy"
    finally:
        lp.lock.release()


def test_scheduler_thread_starts_and_stops(lago, tmp_path, monkeypatch):
    monkeypatch.setenv("BILLING_OUTCOME_INTERVAL_S", "0.05")
    lp, _ = _boot(tmp_path)
    th = loop_api.start_scheduler(lp)
    try:
        assert th is not None and th.is_alive()
    finally:
        loop_api.stop_scheduler()
    assert not th.is_alive()


def test_app_startup_resumes_parked_missions(lago, tmp_path, monkeypatch, load_app):
    monkeypatch.setenv("BILLING_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "op:secret")
    monkeypatch.delenv("BILLING_OUTCOME_INTERVAL_S", raising=False)
    mid = _decide(_boot(tmp_path)[0]).mission_id
    loop_api.set_loop(None)
    try:
        with TestClient(load_app("billing").app) as c:                 # lifespan => startup hook => resume
            assert mid in loop_api.get_loop().launched
            r = c.post(f"/api/decide/{mid}/approve", headers=AUTH, json={})
            assert r.status_code == 200 and r.json()["state"] == "succeeded"
    finally:
        loop_api.set_loop(None)
