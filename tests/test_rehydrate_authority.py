"""Rehydrate restores recorded tenant, budget, policy and grant changes; the HTTP service restores in-flight missions."""
import pytest

from agentic_os.mission.policy import Effect, MissionPolicy, PolicyRule
from agentic_os.mission.service import build_pilot_runtime
from agentic_os.mission.types import Budget


def _rt(path):
    return build_pilot_runtime(str(path))


def test_tenant_and_budget_round_trip(tmp_path):
    p = tmp_path / "e.jsonl"
    rt = _rt(p)
    m = rt.create_mission("onboard customer", template="onboarding", policy_refs=["*"],
                          tenant="acme", budget=Budget(usd=50.0))
    rt.run(m.id)
    m2 = _rt(p).rehydrate(m.id)
    assert m2.tenant == "acme"
    assert m2.budget.usd == 50.0
    assert m2.default_failure_policy == m.default_failure_policy


def test_revoked_grants_stay_revoked(tmp_path):
    p = tmp_path / "e.jsonl"
    rt = _rt(p)
    m = rt.create_mission("onboard customer", template="onboarding",
                          policy_refs=["billing:write", "support:write", "books:write", "compliance:write"])
    rt.change_policy(m.id, actor="ops", reason="tighten", revoke=["billing:write"])
    rt.change_policy(m.id, actor="ops", reason="add", grant=["extra:read"])
    m2 = _rt(p).rehydrate(m.id)
    assert "billing:write" not in m2.policy_refs
    assert sorted(m2.policy_refs) == sorted(m.policy_refs)


def test_mission_policy_round_trip(tmp_path):
    p = tmp_path / "e.jsonl"
    pol = MissionPolicy(id="p", rules=(PolicyRule("r1", Effect.DENY, "side_effecting", reason="no"),))
    rt = _rt(p)
    m = rt.create_mission("onboard customer", template="onboarding", policy_refs=["*"], policy=pol)
    rt.run(m.id)
    m2 = _rt(p).rehydrate(m.id)
    assert m2.policy == pol
    assert m2.state == m.state


def test_policy_digest_mismatch_fails_closed(tmp_path):
    p = tmp_path / "e.jsonl"
    pol = MissionPolicy(id="p", rules=(PolicyRule("r1", Effect.DENY, "side_effecting"),))
    rt = _rt(p)
    m = rt.create_mission("onboard customer", template="onboarding", policy_refs=["*"], policy=pol)
    other = MissionPolicy(id="p", rules=())
    from agentic_os.mission.runtime import ReplayError
    with pytest.raises(ReplayError):
        _rt(p).rehydrate(m.id, policy=other)


def test_service_restores_in_flight_missions(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "t")
    monkeypatch.setenv("MISSION_EVENT_LOG", str(tmp_path / "e.jsonl"))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentic_os.mission.service import mount
    H = {"Authorization": "Bearer t"}
    a = FastAPI()
    mount(a)
    r = TestClient(a).post("/missions", json={"goal": "onboard", "template": "onboarding",
                                              "policy_refs": ["*"]}, headers=H).json()
    mid, node = r["id"], r["pending_human"]["node_id"]
    b = FastAPI()
    mount(b)
    c = TestClient(b)
    assert c.get(f"/missions/{mid}", headers=H).status_code == 200
    assert c.post(f"/missions/{mid}/approve", json={"node_id": node}, headers=H).status_code == 200


def _mark_legacy(path, drop):
    import json
    lines = [json.loads(x) for x in open(path)]
    for d in lines:
        if d["type"] == "MissionCreated":
            for k in drop:
                d["payload"].pop(k, None)
    with open(path, "w") as fh:
        fh.write("".join(json.dumps(d) + "\n" for d in lines))


def test_service_starts_and_quarantines_unrestorable_missions(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentic_os.mission.service import mount, rehydrate_in_flight, RehydrateFailed
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "t")
    monkeypatch.delenv("AGENTIC_OS_REHYDRATE_STRICT", raising=False)
    p = tmp_path / "e.jsonl"
    monkeypatch.setenv("MISSION_EVENT_LOG", str(p))
    H = {"Authorization": "Bearer t"}
    a = FastAPI()
    mount(a)
    ca = TestClient(a)
    good = ca.post("/missions", json={"goal": "onboard", "template": "onboarding", "policy_refs": ["*"]}, headers=H).json()
    old = ca.post("/missions", json={"goal": "onboard", "template": "onboarding", "policy_refs": ["*"]}, headers=H).json()
    lines = [__import__("json").loads(x) for x in open(p)]
    for d in lines:
        if d["type"] == "MissionCreated" and d["mission_id"] == old["id"]:
            d["payload"].pop("policy_refs")
    open(p, "w").write("".join(__import__("json").dumps(d) + "\n" for d in lines))
    b = FastAPI()
    rt = mount(b)                                  # lenient: starts
    cb = TestClient(b)
    assert cb.get(f"/missions/{good['id']}", headers=H).status_code == 200
    assert cb.get(f"/missions/{old['id']}", headers=H).status_code == 404
    assert old["id"] in rt.quarantined and good["id"] not in rt.quarantined
    assert sum(e.type == "MissionQuarantined" for e in rt.store.for_mission(old["id"])) == 1
    rt2 = build_pilot_runtime(str(p))
    with pytest.raises(RehydrateFailed):
        rehydrate_in_flight(rt2, strict=True)


def test_budget_change_restores_all_fields(tmp_path):
    p = tmp_path / "e.jsonl"
    rt = _rt(p)
    m = rt.create_mission("onboard customer", template="onboarding", policy_refs=["*"])
    rt.change_policy(m.id, actor="ops", reason="cap", budget=Budget(usd=2.0, tokens=1000))
    m2 = _rt(p).rehydrate(m.id)
    assert (m2.budget.usd, m2.budget.tokens) == (2.0, 1000)


def test_concurrent_approvals_dispatch_once(tmp_path):
    import threading
    p = tmp_path / "e.jsonl"
    rt = _rt(p)
    m = rt.create_mission("onboard customer", template="onboarding", policy_refs=["*"])
    rt.run(m.id)
    nid = rt.repo.pending_human(m.id)["node_id"]
    rt2 = _rt(p)
    rt2.rehydrate(m.id)
    res = []

    def go():
        try:
            rt2.approve(m.id, nid, "approve", actor="a")
            res.append("ok")
        except Exception:
            res.append("err")

    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    dispatched = [e.payload.get("node_id") for e in rt2.store.for_mission(m.id) if e.type == "NodeDispatched"]
    assert len(dispatched) == len(set(dispatched))
    assert res.count("ok") == 1


def test_startup_is_roughly_linear(tmp_path):
    import time
    p = tmp_path / "e.jsonl"
    rt = _rt(p)
    for _ in range(300):
        rt.create_mission("onboard customer", template="onboarding", policy_refs=["*"])
    t0 = time.time()
    from agentic_os.mission.service import rehydrate_in_flight
    rt2 = _rt(p)
    assert len(rehydrate_in_flight(rt2)) == 300
    assert time.time() - t0 < 5


def test_late_replay_failure_leaves_no_live_mission(tmp_path, monkeypatch):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentic_os.mission.service import mount
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "t")
    p = tmp_path / "e.jsonl"
    monkeypatch.setenv("MISSION_EVENT_LOG", str(p))
    H = {"Authorization": "Bearer t"}
    a = FastAPI()
    mount(a)
    r = TestClient(a).post("/missions", json={"goal": "onboard", "template": "onboarding",
                                              "policy_refs": ["*"]}, headers=H).json()
    mid, node = r["id"], r["pending_human"]["node_id"]
    lines = [json.loads(x) for x in open(p)]
    for d in lines:
        if d["type"] == "PlanCreated" and "plan_fingerprint" in d["payload"]:
            d["payload"]["plan_fingerprint"] = "tampered"
    open(p, "w").write("".join(json.dumps(d) + "\n" for d in lines))
    b = FastAPI()
    rt = mount(b)
    c = TestClient(b)
    assert mid in rt.quarantined
    assert mid not in rt._missions and mid not in rt._plans
    assert c.get(f"/missions/{mid}", headers=H).status_code == 404
    assert mid not in [x["id"] for x in c.get("/missions", headers=H).json()["missions"]]
    assert mid not in [x["mission_id"] for x in c.get("/inbox", headers=H).json()["inbox"]]
    assert c.post(f"/missions/{mid}/approve", json={"node_id": node}, headers=H).status_code == 404


def test_legacy_budget_is_clamped_to_the_tenant_ceiling(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["x:read"], "budget_usd": 1.5}}))
    p = tmp_path / "e.jsonl"
    rt = _rt(p)
    m = rt.create_mission("onboard customer", template="onboarding", policy_refs=["*"], tenant="acme",
                          budget=Budget(usd=50.0))
    rows = [json.loads(x) for x in open(p)]
    for d in rows:
        d["payload"].pop("budget", None) if d["type"] == "MissionCreated" else None
    open(p, "w").write("".join(json.dumps(d) + "\n" for d in rows))
    assert _rt(p).rehydrate(m.id).budget.usd == 1.5
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"budget_usd": 100}}))
    assert _rt(p).rehydrate(m.id).budget.usd == 5.0          # never above the default
