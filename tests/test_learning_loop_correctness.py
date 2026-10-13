"""Learning-loop correctness (round-4 audit): learn-key identity, delay units, the promotion gate being the
only way the active policy changes, canonical do-nothing label, shadow records not competing, one replay
loop, business value from metadata."""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.attribution import correlate, delay_attribution, derive_outcome
from agentic_os.crm_nba import crm_outcome
from agentic_os.historical_replay import DecisionPoint, LeakageError, replay
from agentic_os.a0_evaluation import run_a0
from agentic_os.intervention_record import (
    ActorType, InMemoryInterventionStore, InterventionRecord, record_human_action, select_and_record,
    shadow_report)
from agentic_os.observation import KnownAtQuality, Observation
from agentic_os.observation_store import InMemoryObservationStore
from agentic_os.outcome_derivation import derive_and_persist
from agentic_os.outcome_learning import UtilityModel
from agentic_os.outcome_store import FileOutcomeStore, InMemoryOutcomeStore
from agentic_os.outreach_nba import outreach_outcome
from agentic_os.policy_promotion import (
    FilePolicyStore, PolicyRegistry, PromotionDecision, PromotionGate, PromotionRefused, build_candidate,
    evaluate_candidate, open_policy_store, run_promotion_cycle)
from agentic_os.priority_engine import (
    DO_NOTHING, DecisionOpportunity, InterventionCandidate, OutcomeEvent, OutcomeLog, do_nothing,
    normalize_action_label, select_action)
from agentic_os.shadow_evaluation import evaluate_shadow, run_shadow_step
from agentic_os.wealth_drift import wealth_outcome
from agentic_os.analytics_anomaly import analytics_outcome

HOUR = 3600.0


def _opp(subject="Acme", app="crm", a=0.8, b=0.2, kinds=("send_proposal", "schedule_call")):
    cands = tuple(InterventionCandidate(app, subject, k, v, 0.9, action_kind=k, risk_tier=RiskTier.READ,
                                        candidate_id=f"{app}:{subject}:{k}")
                  for k, v in zip(kinds, (a, b)))
    return DecisionOpportunity(entity=subject, source_app=app, candidate_actions=cands,
                               opportunity_id=f"{app}:{subject}")


def _obs(subject, valid_at, source="whatsapp_waha", payload=None, oid="oc-1"):
    return Observation(oid, source, "chat.message.received", subject, valid_at=valid_at, known_at=valid_at,
                       ingested_at=valid_at, known_at_quality=KnownAtQuality.OBSERVED, payload=payload or {})


# ── 1. learn-key identity ────────────────────────────────────────────────────────────
def test_derived_outcome_trains_the_producers_learn_key_not_the_connector():
    ivs = InMemoryInterventionStore()
    sel, rec = select_and_record(_opp(), ivs, policy_version="p1", proposed_at=0.0)
    assert rec.source_app == "crm" and rec.learn_key == sel.action.learn_key
    obs = _obs("Acme", 2 * HOUR, source="whatsapp_waha")
    att = correlate(obs, ivs.all(), half_life_seconds=HOUR)
    ev, _ = derive_outcome(obs, att, reward_dimensions={"reply": 1.0})
    assert (ev.source_app, ev.action_kind) == sel.action.learn_key          # NOT ("whatsapp_waha", ...)
    model = UtilityModel().fit(OutcomeLog(events=[ev] * 10))
    assert model.utility(sel.action, 0.1) != 0.1                            # the outcome moves THIS candidate


def test_learn_key_matches_for_candidates_without_action_kind():
    long_action = "Send the renewal proposal with the updated pricing sheet"
    c = InterventionCandidate("crm", "Acme", long_action, 0.7, 0.9, risk_tier=RiskTier.READ, candidate_id="crm:Acme:x")
    opp = DecisionOpportunity(entity="Acme", source_app="crm", candidate_actions=(c,), opportunity_id="crm:Acme")
    ivs = InMemoryInterventionStore()
    sel, rec = select_and_record(opp, ivs, policy_version="p1", proposed_at=0.0)
    if sel.action.candidate_id == "crm:Acme:x":
        assert rec.learn_key == c.learn_key                                 # [:24] truncation, not [:40]


def test_legacy_records_without_source_app_keep_old_behaviour():
    rec = InterventionRecord("iv1", "crm:Acme", "crm:Acme:send", "send_proposal", (), (), "p1", 0.5, 0.0, executed_at=0.0)
    assert rec.source_app == ""
    obs = _obs("Acme", HOUR, source="crm")
    att = correlate(obs, [rec], half_life_seconds=HOUR)
    ev, _ = derive_outcome(obs, att, reward_dimensions={"reply": 1.0})
    assert (ev.source_app, ev.action_kind) == ("crm", "send_proposal")


def test_source_app_survives_file_store_roundtrip(tmp_path):
    from agentic_os.intervention_record import FileInterventionStore
    st = FileInterventionStore(str(tmp_path / "iv.jsonl"))
    _, rec = select_and_record(_opp(), st, policy_version="p1", proposed_at=0.0)
    back = st.all()[0]
    assert (back.source_app, back.action_kind, back.shadow, back.prior_score) == (
        rec.source_app, rec.action_kind, False, rec.prior_score) and rec.prior_score is not None


def test_derive_and_persist_end_to_end_adjusts_selection():
    ivs, outs = InMemoryInterventionStore(), InMemoryOutcomeStore()
    for i in range(12):
        _, rec = select_and_record(_opp(f"A{i}"), ivs, policy_version="p1", proposed_at=float(i * 1000),
                                   id_fn=lambda i=i: f"iv{i}")
        derive_and_persist(_obs(f"A{i}", i * 1000 + 60.0, payload={"outcome": "negative_reply"}, oid=f"o{i}"),
                           intervention_store=ivs, outcome_store=outs)
    log = OutcomeLog(events=outs.load())
    model = UtilityModel().fit(log)
    base_sel = select_action(_opp("Z"))
    learned_sel = select_action(_opp("Z"), utility_fn=model.as_utility_fn())
    assert base_sel.action.action_kind == "send_proposal"
    assert learned_sel.action.action_kind != "send_proposal"                # real outcomes changed selection


# ── 2. delay units ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("fn,kw", [
    (crm_outcome, {"replied": True}), (outreach_outcome, {"positive_reply": True}),
    (wealth_outcome, {"plan_improved": True}), (analytics_outcome, {"metric_recovered": True})])
def test_producers_emit_delay_in_seconds_with_shared_attribution(fn, kw):
    c = InterventionCandidate("x", "S", "act", 0.5, 0.9, action_kind="act", candidate_id="x:S:act")
    ev = fn(c, delay_hours=2.0, **kw)
    assert ev.delay == pytest.approx(2 * HOUR)
    near, far = fn(c, delay_hours=1.0, **kw), fn(c, delay_hours=2000.0, **kw)
    assert near.attribution_confidence > far.attribution_confidence > 0.0


def test_delay_attribution_is_the_shared_multifactor_function():
    assert delay_attribution(0.0, half_life_seconds=HOUR) > delay_attribution(5 * HOUR, half_life_seconds=HOUR)
    # same delay, longer half-life (slow outcome type) => more attributable
    assert delay_attribution(HOUR, half_life_seconds=30 * 24 * HOUR) > delay_attribution(HOUR, half_life_seconds=HOUR)


# ── 3/4. promotion gate is the only path; registry persists ───────────────────────────────
def _structured_events(n=60, with_prior=None):
    evs = []
    for i in range(n):
        k = "a" if i % 2 == 0 else "b"
        evs.append(OutcomeEvent(candidate_id=f"c{i}", source_app="ref", action_kind=k,
                                observed_reward=(-0.5 if k == "a" else 0.9), attribution_confidence=1.0,
                                prior_utility=with_prior(k) if with_prior else None))
    return evs


def test_promote_requires_a_passed_gate_result():
    reg = PolicyRegistry()
    cand = build_candidate(_structured_events())
    with pytest.raises(PromotionRefused):
        reg.promote(cand)
    with pytest.raises(PromotionRefused):
        reg.promote(cand, PromotionDecision(False, ("nope",)))
    with pytest.raises(PromotionRefused):
        reg.promote(cand, PromotionDecision(True, ("ok",), candidate_version="other"))
    assert reg.active.kind == "static"
    reg.promote(cand, PromotionDecision(True, ("ok",), candidate_version=cand.version))
    assert reg.active is cand


def test_registry_persists_and_rehydrates(tmp_path):
    path = str(tmp_path / "pol.jsonl")
    reg = PolicyRegistry(store=FilePolicyStore(path))
    store = InMemoryOutcomeStore()
    for e in _structured_events():
        store.append(e)
    rep = run_promotion_cycle(store, registry=reg, apply=True, gate=PromotionGate(min_test_events=5))
    assert rep.promoted
    fresh = PolicyRegistry(store=FilePolicyStore(path))                    # a new process
    assert fresh.active.version == reg.active.version and fresh.active.kind == "learned"
    assert fresh.active.model.observed(("ref", "b")) == reg.active.model.observed(("ref", "b"))
    assert len(fresh.history) == 1
    # a promotion made elsewhere becomes visible on refresh()
    other = PolicyRegistry(store=FilePolicyStore(path))
    assert other.refresh().version == reg.active.version


def test_open_policy_store_defaults(tmp_path, monkeypatch):
    for k in ("POLICY_STORE_PATH", "POLICY_DATABASE_URL", "AOS_DATA_DIR"):
        monkeypatch.delenv(k, raising=False)
    from agentic_os.policy_promotion import InMemoryPolicyStore
    assert isinstance(open_policy_store(), InMemoryPolicyStore)
    monkeypatch.setenv("AOS_DATA_DIR", str(tmp_path))
    st = open_policy_store()
    assert isinstance(st, FilePolicyStore) and st.path.startswith(str(tmp_path))


def test_dry_run_and_refused_gate_do_not_change_policy():
    reg = PolicyRegistry()
    store = InMemoryOutcomeStore()
    for e in _structured_events():
        store.append(e)
    assert run_promotion_cycle(store, registry=reg, apply=False, gate=PromotionGate(min_test_events=5)).promoted is False
    assert run_promotion_cycle(store, registry=reg, apply=True).promoted is False       # default gate: too little data
    assert reg.active.kind == "static"


def test_evaluation_baseline_is_the_selection_prior_when_recorded():
    # the producer prior already predicted each reward exactly: nothing left for learning to add
    exact = lambda k: -0.5 if k == "a" else 0.9
    with_prior = evaluate_candidate(_structured_events(with_prior=exact))
    pooled = evaluate_candidate(_structured_events())
    assert with_prior.baseline == "selection_prior" and pooled.baseline == "pooled"
    assert with_prior.static_mse == pytest.approx(0.0, abs=1e-9)
    assert pooled.relative_improvement > 0.5 > with_prior.relative_improvement
    assert PromotionGate(min_test_events=5).decide(with_prior).promote is False


def test_priorities_do_not_refit_per_request_and_consult_the_registry():
    from fastapi.testclient import TestClient
    from agentic_os.projects_api import SampleProjectionProvider, create_app
    prov = SampleProjectionProvider()
    c = TestClient(create_app(prov))
    for _ in range(30):
        prov.record_outcome_event(source_app="crm", action_kind="send_proposal", observed_reward=-0.6)
        prov.record_outcome_event(source_app="crm", action_kind="schedule_call", observed_reward=0.9)
    s = c.get("/api/projects/customer-ops/priorities").json()
    assert s["learning"]["selection_adjusted"] is False and s["learning"]["outcomes_recorded"] == 60
    # promoting through the registry (and only then) is what the next request reads
    cand = build_candidate(list(prov.outcome_events))
    prov.policy_registry.promote(cand, PromotionDecision(True, ("ok",), candidate_version=cand.version))
    s2 = c.get("/api/projects/customer-ops/priorities").json()
    assert s2["learning"]["selection_adjusted"] is True and s2["learning"]["policy_version"] == cand.version


def test_provider_selection_is_explicit(monkeypatch):
    from agentic_os import projects_api as pa
    monkeypatch.setattr(pa, "registered_live_sources", lambda registry=None: [])
    monkeypatch.delenv("PROJECTS_PROVIDER", raising=False)
    assert type(pa.select_provider()) is pa.SampleProjectionProvider        # nothing registered -> sample
    assert pa.SampleProjectionProvider().priorities("p")["basis"] == "sample"
    monkeypatch.setenv("PROJECTS_PROVIDER", "registered")
    prov = pa.select_provider()
    assert isinstance(prov, pa.RegisteredProducersProvider)
    assert prov.priorities("p")["basis"] == "sample"                        # no live source -> labelled fallback
    monkeypatch.setenv("PROJECTS_PROVIDER", "bogus")
    with pytest.raises(ValueError):
        pa.select_provider()


def test_registered_producers_provider_aggregates_live_sources():
    from types import SimpleNamespace as NS
    from agentic_os import projects_api as pa
    fake = [NS(manifest=NS(name="billing", producers=(NS(name="dunning"), NS(name="no-source"))))]
    cand = InterventionCandidate("billing", "Acme invoice", "Chase overdue invoice", 0.8, 0.9,
                                 risk_tier=RiskTier.CONSEQUENTIAL, candidate_id="billing:inv-1",
                                 action_kind="chase")
    pa.register_live_producer_source("billing", "dunning", lambda: [cand])
    try:
        assert [(a, p) for a, p, _ in pa.registered_live_sources(fake)] == [("billing", "dunning")]
        prov = pa.RegisteredProducersProvider(registry=fake)
        out = prov.priorities("p")
        ids = {i["candidate_id"] for i in out["surfaced"] + out["deferred"]}
        assert out["basis"] == "registered" and "billing:inv-1" in ids
        assert out["producers"] == ["billing/dunning"]
    finally:
        pa.unregister_live_producer_source("billing", "dunning")


# ── 5. promoted_choice is read on the plan-selection path ─────────────────────────────────
def test_promoted_routing_choice_steers_plan_selection_but_recommendation_alone_does_not():
    from tests.test_active_planning import _P, _two_provider_fleet
    from agentic_os.mission.executor import Executor
    from agentic_os.mission.runtime import MissionRuntime
    reg, client = _two_provider_fleet()
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m0 = rt.create_mission("act", policy_refs=["*"])
    default_sig = rt._signature(rt._plans[m0.id])
    other = "provB.do" if "provA.do" in default_sig else "provA.do"
    for _ in range(6):
        rt.learners.routing.observe("done", other, True)
        rt.learners.routing.observe("done", default_sig, False)
    m1 = rt.create_mission("act", policy_refs=["*"])
    assert rt._signature(rt._plans[m1.id]) == default_sig                   # recommended != promoted: shadow only
    rt.learners.promote(type("R", (), {"kind": "routing", "subject": "done", "recommended": other, "status": ""})())
    m2 = rt.create_mission("act", policy_refs=["*"])
    assert rt._signature(rt._plans[m2.id]) == other
    ev = [e for e in rt.repo.timeline(m2.id) if e["type"] == "PlanSelected"][0]
    assert ev["payload"]["promoted_choice"] == f"routing/done={other}"


# ── 6. business value from metadata ─────────────────────────────────────────────────────
def test_business_value_comes_from_metadata_with_constants_as_defaults(monkeypatch):
    from types import SimpleNamespace as NS
    from agentic_os.mission.runtime import business_value_for
    monkeypatch.delenv("AGENTIC_OS_BUSINESS_VALUE", raising=False)
    assert business_value_for("onboarding", registry=[]) == 100.0           # default constant
    assert business_value_for("custom_kind", registry=[]) == 0.0
    reg = [NS(manifest=NS(outcome_kinds=(NS(key="custom_kind", value_usd=42.0),)))]
    assert business_value_for("custom_kind", registry=reg) == 42.0           # OutcomeKind metadata
    monkeypatch.setenv("AGENTIC_OS_BUSINESS_VALUE", '{"custom_kind": 7, "onboarding": 5}')
    assert business_value_for("onboarding", registry=[]) == 5.0              # config beats constant
    assert business_value_for("custom_kind", NS(inputs={"business_value_usd": 9}), registry=reg) == 9.0
    assert business_value_for("custom_kind", overrides={"custom_kind": 1.5}, registry=reg) == 1.5


# ── 7. leakage violations are counted in 'count' mode, fail-closed by default ──────────────
def _leaky_builder(subject, obs):
    return DecisionOpportunity(
        entity=subject, source_app="outreach", opportunity_id=f"outreach:{subject}",
        candidate_actions=(InterventionCandidate("outreach", subject, "contact", 0.8, 0.9, action_kind="contact",
                                                 risk_tier=RiskTier.READ, observation_refs=("future",),
                                                 candidate_id=f"outreach:{subject}:contact"),))


def _fut():
    return _obs("Acme", 500.0, oid="future")


def test_replay_counts_leakage_violations_and_stays_fail_closed_by_default():
    pts = [DecisionPoint(100.0, "Acme", known_good_action="contact"), DecisionPoint(100.0, "Acme", "contact")]
    with pytest.raises(LeakageError):
        replay([_fut()], pts, _leaky_builder)
    rep = replay([_fut()], pts, _leaky_builder, leakage="count")
    assert rep.gate_violations == 2 and rep.evaluated == 0 and len(rep.leakage_details) == 2


def test_a0_counts_leakage_violations():
    st = InMemoryObservationStore()
    st.append(_obs("Acme", 10.0, oid="past"))
    pts = [DecisionPoint(100.0, "Acme", known_good_action="contact")]
    with pytest.raises(LeakageError):
        run_a0(st, pts, _leaky_builder)
    assert run_a0(st, pts, _leaky_builder, leakage="count").gate_violations == 1


# ── 8. one canonical do-nothing label ────────────────────────────────────────────────────
def _weak_builder(subject, obs):
    c = InterventionCandidate("outreach", subject, "contact", -0.5, 0.9, action_kind="contact",
                              risk_tier=RiskTier.CONSEQUENTIAL, candidate_id=f"outreach:{subject}:contact")
    return DecisionOpportunity(entity=subject, source_app="outreach", candidate_actions=(c,),
                               opportunity_id=f"outreach:{subject}")


@pytest.mark.parametrize("label", ["do nothing", "do_nothing", DO_NOTHING, "Abstain", "do-nothing"])
def test_abstention_agrees_with_a_human_who_also_did_nothing(label):
    obs = [_obs("Acme", 10.0, oid="e1")]
    rep = replay(obs, [DecisionPoint(50.0, "Acme", known_good_action=label)], _weak_builder)
    assert rep.evaluated == 1 and rep.agreement == 1
    st = InMemoryObservationStore()
    st.append(obs[0])
    assert run_a0(st, [DecisionPoint(50.0, "Acme", known_good_action=label)], _weak_builder).agreement == 1


def test_do_nothing_candidate_carries_the_canonical_kind_and_shadow_agrees():
    assert do_nothing("x").action_kind == DO_NOTHING and do_nothing("x").learn_key[1] == DO_NOTHING
    assert normalize_action_label("do nothing") == normalize_action_label("Abstain") == DO_NOTHING
    ivs = InMemoryInterventionStore()
    run_shadow_step(_weak_builder("Acme", []), ivs, human_action="do nothing", policy_version="p",
                    proposed_at=1.0, human_at=2.0)
    pair = shadow_report(ivs).pairs[0]
    assert pair.runtime_action == DO_NOTHING and pair.agreed is True
    assert evaluate_shadow(ivs, InMemoryOutcomeStore()).agreement_rate == 1.0


# ── 9. shadow records do not compete; subject matching is exact ──────────────────────────
def test_shadow_recommendations_are_excluded_from_competing_interventions():
    ivs = InMemoryInterventionStore()
    run_shadow_step(_opp("Acme", app="outreach", kinds=("contact", "wait")), ivs, human_action="contact",
                    policy_version="p", proposed_at=10.0, human_at=20.0)
    assert any(r.shadow for r in ivs.all())
    att = correlate(_obs("Acme", 30.0), ivs.all(), half_life_seconds=HOUR)
    assert att.intervention.actor_type is ActorType.HUMAN
    assert att.factors.competing_interventions == 0.0 and len(att.candidate_ids) == 1


def test_a_real_earlier_runtime_action_still_competes():
    ivs = InMemoryInterventionStore()
    select_and_record(_opp("Acme"), ivs, policy_version="p", proposed_at=5.0)
    record_human_action(ivs, opportunity_id="crm:Acme", action_kind="call", at=20.0)
    att = correlate(_obs("Acme", 30.0), ivs.all(), half_life_seconds=HOUR)
    assert att.factors.competing_interventions > 0


def test_subject_matching_is_exact_not_substring():
    ivs = InMemoryInterventionStore()
    select_and_record(_opp("Acme Corp"), ivs, policy_version="p", proposed_at=5.0)
    assert correlate(_obs("Acme", 30.0), ivs.all(), half_life_seconds=HOUR) is None      # not a substring hit
    assert correlate(_obs("acme  corp", 30.0), ivs.all(), half_life_seconds=HOUR) is not None   # normalised equality
    assert correlate(_obs("crm:Acme Corp", 30.0), ivs.all(), half_life_seconds=HOUR) is not None


# ── 10. no duplicated loops ───────────────────────────────────────────────────────────────
def test_run_a0_is_a_thin_wrapper_over_replay():
    st = InMemoryObservationStore()
    st.append(_obs("Acme", 10.0, oid="e1"))
    pts = [DecisionPoint(50.0, "Acme", known_good_action="contact"), DecisionPoint(50.0, "Nobody", "x")]
    assert run_a0(st, pts, _weak_builder) == replay(st, pts, _weak_builder)
    import inspect
    from agentic_os import a0_evaluation
    assert "select_action" not in inspect.getsource(a0_evaluation.run_a0)


def test_shadow_report_and_evaluate_shadow_share_the_pairing():
    import inspect
    from agentic_os import intervention_record
    assert "pair_actions" in inspect.getsource(intervention_record.shadow_report)
