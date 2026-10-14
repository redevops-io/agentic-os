"""P0: POST /api/outcomes must never let an anonymous caller create learning evidence.
Anonymous => sandboxed demo namespace; authoritative => bearer token + executed-intervention receipt."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentic_os.intervention_record import InMemoryInterventionStore, InterventionRecord
from agentic_os.outcome_store import FileOutcomeStore, InMemoryOutcomeStore, event_from_dict, event_to_dict
from agentic_os.policy_promotion import PromotionGate, run_promotion_cycle
from agentic_os.priority_engine import OutcomeEvent
from agentic_os.projects_api import DemoOutcomeSandbox, SampleProjectionProvider, create_app

TOKEN = "t0ken-for-tests"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
GATE = PromotionGate(min_test_events=5, min_keys_covered=2, min_relative_improvement=0.0)


def _rec(iid, *, executed=True, shadow=False, ref="exec-1", mission="m1", app="crm", kind="send_proposal"):
    return InterventionRecord(
        intervention_id=iid, opportunity_id="o", candidate_id="cand", selected_action=kind, alternatives=(),
        evidence_refs=(), policy_version="v0", score=0.1, proposed_at=1.0,
        executed_at=1.0 if executed else None, execution_ref=ref if executed else "", mission_id=mission,
        source_app=app, action_kind=kind, shadow=shadow)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", TOKEN)
    istore = InMemoryInterventionStore()
    istore.append(_rec("i-ok"))
    istore.append(_rec("i-pending", executed=False))
    istore.append(_rec("i-shadow", shadow=True))
    path = str(tmp_path / "o.jsonl")
    prov = SampleProjectionProvider(persist_path=path, intervention_store=istore,
                                    policy_store_path=str(tmp_path / "p.jsonl"))
    return prov, TestClient(create_app(prov)), path


def _body(**kw):
    b = {"intervention_id": "i-ok", "idempotency_key": "k1", "observed_reward": 0.9,
         "attribution_confidence": 0.8, "authoritative": True}
    b.update(kw)
    return b


# ── anonymous => demo, never evidence ───────────────────────────────────────────────
def test_anonymous_post_is_demo_and_changes_nothing(env):
    prov, c, path = env
    r = c.post("/api/outcomes", json={"source_app": "crm", "action_kind": "send_proposal",
                                      "observed_reward": -1.0, "attribution_confidence": 1.0}).json()
    assert r["demo"] is True and r["simulated"] is True and r["namespace"] == "demo"
    assert r["authoritative"] is False and r["durable"] is False
    assert prov.outcome_events == [] and FileOutcomeStore(path).load() == []
    import os
    assert not os.path.exists(path)


def test_demo_events_never_change_a_promotion_decision(env):
    prov, c, _ = env
    for _ in range(20):                                    # real evidence: schedule_call beats send_proposal
        prov.record_outcome_event(source_app="crm", action_kind="send_proposal", observed_reward=-0.6)
        prov.record_outcome_event(source_app="crm", action_kind="schedule_call", observed_reward=0.9)
    before = prov.run_promotion_cycle(apply=False, gate=GATE)
    for n in range(300):                                   # flood with adversarial demo events
        c.post("/api/outcomes", headers={"X-Demo-Session": f"s{n % 7}"},
               json={"source_app": "crm", "action_kind": "schedule_call", "observed_reward": -50.0})
        c.post("/api/outcomes", json={"source_app": "crm", "action_kind": "send_proposal",
                                      "observed_reward": 50.0})
    after = prov.run_promotion_cycle(apply=False, gate=GATE)
    assert (after.decision.promote, after.candidate.version, after.result.relative_improvement,
            after.result.n_test) == (before.decision.promote, before.candidate.version,
                                     before.result.relative_improvement, before.result.n_test)
    # and with NO real evidence, demo floods can never produce a promotable policy
    prov2 = SampleProjectionProvider()
    c2 = TestClient(create_app(prov2))
    for _ in range(100):
        c2.post("/api/outcomes", json={"source_app": "crm", "action_kind": "x", "observed_reward": 1.0})
    assert prov2.run_promotion_cycle(apply=True, gate=GATE).promoted is False


def test_simulated_events_are_refused_by_stores_and_filtered_by_cycle(tmp_path):
    sim = OutcomeEvent(candidate_id="c", source_app="crm", action_kind="a", observed_reward=1.0, simulated=True)
    for st in (InMemoryOutcomeStore(), FileOutcomeStore(str(tmp_path / "x.jsonl"))):
        with pytest.raises(ValueError):
            st.append(sim)
    class Leaky:                                            # a store that somehow holds simulated events
        def load(self):
            return [OutcomeEvent(candidate_id="c", source_app="crm", action_kind="a", observed_reward=1.0,
                                 simulated=True)] * 50
    rep = run_promotion_cycle(Leaky(), gate=GATE)
    assert rep.result.n_test == 0 and rep.promoted is False
    prov = SampleProjectionProvider()
    prov.outcome_events.append(sim)
    assert prov.run_promotion_cycle(gate=GATE).result.n_test == 0


def test_demo_sandbox_cap_ttl_and_session_isolation():
    t = [0.0]
    sb = DemoOutcomeSandbox(cap_per_session=3, max_sessions=2, ttl_seconds=10, clock=lambda: t[0])
    assert all(sb.add("a", {"x": i})["stored"] for i in range(3))
    assert sb.add("a", {"x": 9})["stored"] is False             # cap
    assert sb.add("b", {})["stored"] is True
    assert sb.add("c", {})["stored"] is True                     # capacity: oldest session evicted, not refused
    assert sb.events("a") == [] and len(sb._sessions) == 2
    assert all(e["demo"] and e["simulated"] and e["namespace"] == "demo" for e in sb.events("a"))
    assert sb.events("b") != sb.events("a")
    t[0] = 20.0                                                  # TTL
    assert sb.events("b") == [] and sb.add("d", {})["stored"] is True


def test_demo_cap_via_http(env):
    _, c, _ = env
    outs = [c.post("/api/outcomes", headers={"X-Demo-Session": "z"}, json={"observed_reward": 1}).json()
            for _ in range(55)]
    assert sum(o["stored"] for o in outs) == 50 and outs[-1]["stored"] is False and outs[-1]["demo"] is True


# ── authoritative path: auth ────────────────────────────────────────────────────────
def test_authoritative_without_configured_token_is_503(env, monkeypatch):
    prov, c, path = env
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN")
    assert c.post("/api/outcomes", json=_body()).status_code == 503
    assert c.post("/api/outcomes", headers=AUTH, json=_body(authoritative=False)).status_code == 503
    assert prov.outcome_events == []


@pytest.mark.parametrize("hdr", [{"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}, {}])
def test_authoritative_wrong_or_missing_token_is_401(env, hdr):
    prov, c, _ = env
    assert c.post("/api/outcomes", headers=hdr, json=_body()).status_code == 401
    assert prov.outcome_events == []


# ── authoritative path: receipts ────────────────────────────────────────────────────
@pytest.mark.parametrize("override,code", [
    ({"intervention_id": ""}, 422), ({"intervention_id": "ghost"}, 404),
    ({"intervention_id": "i-pending"}, 409), ({"intervention_id": "i-shadow"}, 409),
    ({"execution_ref": "forged"}, 409), ({"mission_id": "other"}, 409), ({"source_app": "evil"}, 409),
    ({"idempotency_key": ""}, 422), ({"attribution_confidence": None}, 422),
    ({"attribution_confidence": 1.5}, 422), ({"attribution_confidence": -0.1}, 422),
    ({"observed_reward": None}, 422),
])
def test_authoritative_bad_receipt_or_fields_rejected_no_append(env, override, code):
    prov, c, path = env
    r = c.post("/api/outcomes", headers=AUTH, json=_body(**override))
    assert r.status_code == code, r.text
    assert prov.outcome_events == [] and FileOutcomeStore(path).load() == []


def test_authoritative_success_derives_identity_from_receipt_and_is_idempotent(env):
    prov, c, path = env
    from dataclasses import replace
    prov.intervention_store.append(replace(_rec("i-acme"), tenant_id="acme"))
    prov.intervention_store.append(replace(_rec("i-beta"), tenant_id="beta"))
    r1 = c.post("/api/outcomes", headers=AUTH, json=_body(intervention_id="i-acme", tenant_id="acme", execution_ref="exec-1",
                                                           mission_id="m1", source_app="crm"))
    assert r1.status_code == 200, r1.text
    j1 = r1.json()
    assert j1["authoritative"] is True and j1["demo"] is False and j1["recorded"] == 1
    j2 = c.post("/api/outcomes", headers=AUTH, json=_body(intervention_id="i-acme", tenant_id="acme")).json()
    assert j2["duplicate"] is True and j2["recorded"] == 1
    assert len(prov.outcome_events) == 1 and len(FileOutcomeStore(path).load()) == 1
    ev = prov.outcome_events[0]
    assert (ev.source_app, ev.action_kind, ev.candidate_id, ev.selected_intervention_id, ev.tenant_id) == \
        ("crm", "send_proposal", "cand", "i-acme", "acme")
    # same key under another tenant (with that tenant's own receipt) is a different event
    assert c.post("/api/outcomes", headers=AUTH,
                  json=_body(intervention_id="i-beta", tenant_id="beta")).json()["recorded"] == 2


# ── store-level dedupe, tenant round trip + isolation ────────────────────────────────
def test_store_append_dedupes_on_event_id_and_derived_observation_id(tmp_path):
    for st in (InMemoryOutcomeStore(), FileOutcomeStore(str(tmp_path / "d.jsonl"))):
        e = OutcomeEvent(candidate_id="c", source_app="a", event_id="same")
        st.append(e); st.append(e)
        d = OutcomeEvent(candidate_id="c", source_app="a", source_observation_ids=("ob1",),
                         selected_intervention_id="i1")
        st.append(d); st.append(d)
        st.append(OutcomeEvent(candidate_id="c", source_app="a"))      # no key: never deduped
        st.append(OutcomeEvent(candidate_id="c", source_app="a"))
        assert len(st.load()) == 4


def test_tenant_and_event_id_roundtrip_and_legacy_lines_load():
    ev = OutcomeEvent(candidate_id="c", source_app="a", tenant_id="acme", event_id="e")
    assert event_from_dict(event_to_dict(ev)) == ev
    legacy = {k: v for k, v in event_to_dict(ev).items() if k not in ("tenant_id", "event_id")}
    back = event_from_dict(legacy)
    assert back.tenant_id is None and back.event_id == ""


def test_promotion_cycle_is_tenant_scoped():
    prov = SampleProjectionProvider()
    for _ in range(20):
        prov.record_outcome_event(source_app="crm", action_kind="send_proposal", observed_reward=-0.6,
                                  tenant_id="A")
        prov.record_outcome_event(source_app="crm", action_kind="schedule_call", observed_reward=0.9,
                                  tenant_id="A")
    assert prov.run_promotion_cycle(gate=GATE, tenant_id="A").result.n_test > 0
    assert prov.run_promotion_cycle(gate=GATE, tenant_id="B").result.n_test == 0


# ═══ review round 2 ═══════════════════════════════════════════════════════════════
import threading

from agentic_os.intervention_record import FileInterventionStore


def _tenant_env(monkeypatch, tmp_path, tenant):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", TOKEN)
    ist = InMemoryInterventionStore()
    r = _rec("i-t")
    from dataclasses import replace
    ist.append(replace(r, tenant_id=tenant))
    prov = SampleProjectionProvider(intervention_store=ist)
    return prov, TestClient(create_app(prov))


# B1 tenant isolation
@pytest.mark.parametrize("rec_tenant,req_tenant,ok", [
    ("A", "A", True), ("A", "B", False), ("A", None, False), ("A", "", False), (None, "A", False),
    (None, None, True), (None, "", True), ("", None, True), (None, "  ", True)])
def test_receipt_tenant_must_equal_request_tenant(monkeypatch, tmp_path, rec_tenant, req_tenant, ok):
    prov, c = _tenant_env(monkeypatch, tmp_path, rec_tenant)
    r = c.post("/api/outcomes", headers=AUTH, json=_body(intervention_id="i-t", tenant_id=req_tenant))
    assert (r.status_code == 200) == ok, r.text
    assert len(prov.outcome_events) == (1 if ok else 0)
    if not ok:
        assert r.status_code == 409


def test_same_receipt_cannot_mint_events_for_many_tenants(monkeypatch, tmp_path):
    prov, c = _tenant_env(monkeypatch, tmp_path, "A")
    for i, t in enumerate(["A", "B", None, ""]):
        c.post("/api/outcomes", headers=AUTH, json=_body(intervention_id="i-t", tenant_id=t, idempotency_key=f"k{i}"))
    assert [e.tenant_id for e in prov.outcome_events] == ["A"]


def test_intervention_record_tenant_roundtrips_and_legacy_loads(tmp_path):
    from dataclasses import replace
    st = FileInterventionStore(str(tmp_path / "i.jsonl"))
    st.append(replace(_rec("x"), tenant_id="A"))
    assert st.all()[0].tenant_id == "A"
    import json
    line = json.loads(open(st.path).read())
    del line["tenant_id"]
    open(st.path, "w").write(json.dumps(line) + "\n")
    assert st.all()[0].tenant_id is None


# B2 wired from config, end to end through create_app
def test_intervention_store_wired_from_env_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", TOKEN)
    ipath, opath = str(tmp_path / "i.jsonl"), str(tmp_path / "o.jsonl")
    monkeypatch.setenv("INTERVENTION_STORE_PATH", ipath)
    monkeypatch.setenv("OUTCOME_STORE_PATH", opath)
    monkeypatch.setenv("PROJECTS_PROVIDER", "sample")
    c = TestClient(create_app())                           # no provider injected: select_provider wires it
    assert c.post("/api/outcomes", headers=AUTH, json=_body()).status_code == 404     # no receipt yet
    FileInterventionStore(ipath).append(_rec("i-ok"))      # trusted path writes the receipt
    r = c.post("/api/outcomes", headers=AUTH, json=_body())
    assert r.status_code == 200 and r.json()["recorded"] == 1
    assert len(FileOutcomeStore(opath).load()) == 1


def test_intervention_store_unset_stays_fail_closed(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", TOKEN)
    monkeypatch.delenv("INTERVENTION_STORE_PATH", raising=False)
    monkeypatch.setenv("PROJECTS_PROVIDER", "sample")
    monkeypatch.delenv("OUTCOME_STORE_PATH", raising=False)
    assert TestClient(create_app()).post("/api/outcomes", headers=AUTH, json=_body()).status_code == 404


# S1 idempotency payload hash
def test_same_key_different_payload_is_409(env):
    prov, c, _ = env
    assert c.post("/api/outcomes", headers=AUTH, json=_body()).status_code == 200
    r = c.post("/api/outcomes", headers=AUTH, json=_body(observed_reward=-0.9))
    assert r.status_code == 409 and len(prov.outcome_events) == 1
    r2 = c.post("/api/outcomes", headers=AUTH, json=_body())          # identical replay
    assert r2.status_code == 200 and r2.json()["duplicate"] is True


# S2 atomicity
def test_concurrent_duplicate_appends_yield_one_event(tmp_path):
    for st in (InMemoryOutcomeStore(), FileOutcomeStore(str(tmp_path / "c.jsonl"))):
        n, barrier = 16, threading.Barrier(16)
        def work():
            barrier.wait()
            st.append(OutcomeEvent(candidate_id="c", source_app="a", event_id="same"))
        ts = [threading.Thread(target=work) for _ in range(n)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert len(st.load()) == 1


def test_concurrent_http_duplicates_yield_one_event(env):
    prov, c, path = env
    barrier = threading.Barrier(8)
    def work():
        barrier.wait()
        c.post("/api/outcomes", headers=AUTH, json=_body())
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(prov.outcome_events) == 1 and len(FileOutcomeStore(path).load()) == 1


# S3 numeric validation, both lanes
BAD = [{"observed_reward": float("nan")}, {"observed_reward": 1e9}, {"observed_reward": -1e9},
       {"reward_dimensions": {"a": 1e12}}, {"reward_dimensions": {f"k{i}": 1.0 for i in range(40)}},
       {"reward_dimensions": {"k" * 200: 1.0}}, {"delay": -1.0}, {"delay": 1e12},
       {"attribution_confidence": 2.0}, {"idempotency_key": "x" * 500}, {"source_app": "s" * 5000}]


@pytest.mark.parametrize("bad", BAD)
def test_bad_numbers_rejected_on_both_lanes(env, bad):
    prov, c, _ = env
    demo_bad = {k: v for k, v in bad.items() if k != "idempotency_key"}
    import json as _j
    raw = lambda d: _j.dumps(d, allow_nan=True)
    h = {"Content-Type": "application/json"}
    assert c.post("/api/outcomes", headers={**AUTH, **h}, content=raw(_body(**bad))).status_code == 422
    assert prov.outcome_events == []
    if demo_bad:
        assert c.post("/api/outcomes", headers=h, content=raw({"observed_reward": 1, **demo_bad})).status_code == 422
    assert all(sum(len(v) for v in [s]) == 0 for s in c.app.state.demo_outcomes._sessions.values())


def test_oversized_body_rejected_413(env):
    _, c, _ = env
    r = c.post("/api/outcomes", json={"source_app": "x", "reward_dimensions": {f"k{i}": 1 for i in range(10)},
                                      "note": "y" * 40000})
    assert r.status_code == 413


# S4 promotion route
def test_promotion_route_apply_requires_token_and_tenant_scoped(env, monkeypatch):
    prov, c, _ = env
    monkeypatch.setenv("PROJECTS_ALLOW_POLICY_APPLY", "1")
    url = "/api/projects/customer-ops/policy/promotion-cycle"
    for _ in range(20):
        prov.record_outcome_event(source_app="crm", action_kind="send_proposal", observed_reward=-0.6, tenant_id="A")
        prov.record_outcome_event(source_app="crm", action_kind="schedule_call", observed_reward=0.9, tenant_id="A")
    assert c.post(url, json={"apply": True}).status_code == 401
    assert c.post(url, json={"tenant_id": "A"}).status_code == 401         # anonymous cannot read tenant A's evidence
    assert c.post(url, json={}).json()["n_test"] == 0                      # anonymous dry run: untenanted only
    assert c.post(url, headers=AUTH, json={"tenant_id": "B"}).json()["n_test"] == 0
    assert c.post(url, headers=AUTH, json={"tenant_id": "A"}).json()["n_test"] > 0
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN")
    assert c.post(url, json={"apply": True}).status_code == 503


# S5 demo spoofing + global budget
def test_rotating_session_header_does_not_widen_allowance(env):
    _, c, _ = env
    outs = [c.post("/api/outcomes", headers={"X-Demo-Session": f"s{i}"}, json={"observed_reward": 1}).json()
            for i in range(130)]
    assert sum(o["stored"] for o in outs) == 100                           # per-client cap, not per header


def test_global_demo_budget():
    sb = DemoOutcomeSandbox(cap_per_session=10, cap_per_group=10, global_cap=15, max_sessions=100)
    for i in range(40):
        assert sb.add(f"t{i}", {}, group=f"g{i}")["stored"]       # evicts oldest, never refuses newcomers
    assert sum(len(sb.events(k)) for k in list(sb._sessions)) <= 15


# S6 pinning derived dedupe
def test_derived_outcome_dedupe_semantics_pinned():
    st = InMemoryOutcomeStore()
    mk = lambda r: OutcomeEvent(candidate_id="c", source_app="a", source_observation_ids=("ob",),
                                selected_intervention_id="i", observed_reward=r)
    st.append(mk(1.0)); st.append(mk(-1.0))     # same observation + same intervention => second is DROPPED
    assert [e.observed_reward for e in st.load()] == [1.0]
    st.append(OutcomeEvent(candidate_id="c", source_app="a", source_observation_ids=("ob",),
                           selected_intervention_id="i2"))
    assert len(st.load()) == 2


# ═══ review round 3 ═══
def test_streamed_body_without_content_length_aborts_early():
    import asyncio
    from agentic_os.projects_api import BodyLimitMiddleware
    sent, pulled = [], [0]

    async def inner(scope, receive, send):
        while True:
            m = await receive()
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def run(path):
        sent.clear(); pulled[0] = 0

        async def receive():
            pulled[0] += 1
            await asyncio.sleep(0)
            return {"type": "http.request", "body": b"x" * 1024, "more_body": pulled[0] < 100000}
        async def send(m): sent.append(m)
        await BodyLimitMiddleware(inner, 16 * 1024)(
            {"type": "http", "method": "POST", "path": path, "headers": []}, receive, send)
    asyncio.run(run("/api/outcomes"))
    assert sent[0]["status"] == 413 and pulled[0] <= 17          # stopped after ~16 KiB, not 100 MB
    asyncio.run(run("/api/projects/p/policy/promotion-cycle"))
    assert sent[0]["status"] == 413 and pulled[0] <= 17
    asyncio.run(run("/api/other"))                               # unguarded path is untouched
    assert sent[0]["status"] == 200 and pulled[0] == 100000


def test_chunked_body_http_413(env):
    _, c, _ = env
    def gen():
        for _ in range(100):
            yield b"x" * 1024
    assert c.post("/api/outcomes", content=gen()).status_code == 413


def test_spoofed_forwarded_header_ignored_by_default_and_honoured_from_trusted_peer(env, monkeypatch):
    _, c, _ = env
    sb = c.app.state.demo_outcomes
    h = {"X-Forwarded-For": "9.9.9.9", "CF-Connecting-IP": "8.8.8.8"}
    c.post("/api/outcomes", headers=h, json={"observed_reward": 1})
    assert not any(k.startswith(("9.9.9.9", "8.8.8.8")) for k in sb._sessions)       # default OFF
    monkeypatch.setenv("OUTCOMES_TRUSTED_PROXY_HEADER", "x-forwarded-for")
    c.post("/api/outcomes", headers=h, json={"observed_reward": 1})
    assert not any(k.startswith("9.9.9.9") for k in sb._sessions)                    # peer not trusted
    monkeypatch.setenv("OUTCOMES_TRUSTED_PROXY_IPS", "testclient")
    c.post("/api/outcomes", headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"}, json={"observed_reward": 1})
    assert any(k.startswith("9.9.9.9|") for k in sb._sessions)                       # last (proxy-added) entry
    monkeypatch.setenv("OUTCOMES_TRUSTED_PROXY_HEADER", "cf-connecting-ip")
    c.post("/api/outcomes", headers=h, json={"observed_reward": 1})
    assert any(k.startswith("8.8.8.8|") for k in sb._sessions)


def test_global_budget_evicts_oldest_instead_of_refusing_everyone():
    t = [0.0]
    sb = DemoOutcomeSandbox(cap_per_session=10, cap_per_group=10, global_cap=5, clock=lambda: t[0])
    for i in range(5):
        t[0] += 1; assert sb.add(f"v{i}", {}, group=f"v{i}")["stored"]
    t[0] += 1
    assert sb.add("new", {}, group="new")["stored"] is True       # a newcomer is still served
    assert sb.events("v0") == [] and sb.events("new")


def test_default_demo_ttl_is_15_minutes():
    assert DemoOutcomeSandbox().ttl == 900
