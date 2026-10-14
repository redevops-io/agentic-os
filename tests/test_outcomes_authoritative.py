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
    assert sb.add("c", {})["stored"] is False                    # session capacity
    assert all(e["demo"] and e["simulated"] and e["namespace"] == "demo" for e in sb.events("a"))
    assert sb.events("b") != sb.events("a")
    t[0] = 11.0                                                  # TTL
    assert sb.events("a") == [] and sb.add("c", {})["stored"] is True


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
    r1 = c.post("/api/outcomes", headers=AUTH, json=_body(tenant_id="acme", execution_ref="exec-1",
                                                           mission_id="m1", source_app="crm"))
    assert r1.status_code == 200, r1.text
    j1 = r1.json()
    assert j1["authoritative"] is True and j1["demo"] is False and j1["recorded"] == 1
    j2 = c.post("/api/outcomes", headers=AUTH, json=_body(tenant_id="acme", observed_reward=-5)).json()
    assert j2["duplicate"] is True and j2["recorded"] == 1
    assert len(prov.outcome_events) == 1 and len(FileOutcomeStore(path).load()) == 1
    ev = prov.outcome_events[0]
    assert (ev.source_app, ev.action_kind, ev.candidate_id, ev.selected_intervention_id, ev.tenant_id) == \
        ("crm", "send_proposal", "cand", "i-ok", "acme")
    # same key under another tenant is a different event
    assert c.post("/api/outcomes", headers=AUTH, json=_body(tenant_id="beta")).json()["recorded"] == 2


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
