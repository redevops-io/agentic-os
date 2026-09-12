"""Tests for shadow evaluation (agentic_os.shadow_evaluation) — prospective, outcome-conditioned."""
from __future__ import annotations

import uuid

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.intervention_record import ActorType, InMemoryInterventionStore
from agentic_os.observation import KnownAtQuality, Observation
from agentic_os.outcome_derivation import derive_and_persist
from agentic_os.outcome_store import InMemoryOutcomeStore
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate
from agentic_os.shadow_evaluation import evaluate_shadow, run_shadow_step


def _opp(subject, **ev):
    vals = {"contact": 0.8, "wait": 0.2}
    vals.update(ev)
    tiers = {"contact": RiskTier.CONSEQUENTIAL, "wait": RiskTier.READ, "investigate": RiskTier.READ}
    cands = tuple(InterventionCandidate("outreach", subject, k, v, 0.8, action_kind=k,
                                        risk_tier=tiers.get(k, RiskTier.CONSEQUENTIAL),
                                        candidate_id=f"outreach:{subject}:{k}")
                  for k, v in vals.items())
    return DecisionOpportunity(entity=subject, source_app="outreach", candidate_actions=cands,
                               opportunity_id=f"outreach:{subject}")


def _reply(subject, valid_at, outcome_label):
    return Observation(f"oc-{uuid.uuid4().hex}", "whatsapp_waha", "chat.message.received", subject,
                       valid_at=valid_at, known_at=valid_at, ingested_at=valid_at,
                       known_at_quality=KnownAtQuality.OBSERVED, payload={"outcome": outcome_label})


def test_run_shadow_step_records_runtime_recommendation_and_human_action():
    ivs = InMemoryInterventionStore()
    sel, rrec, hrec = run_shadow_step(_opp("Acme"), ivs, human_action="contact", policy_version="p1",
                                      proposed_at=10.0, human_at=20.0)
    assert rrec.actor_type is ActorType.RUNTIME and hrec.actor_type is ActorType.HUMAN
    assert hrec.selected_action == "contact" and len(ivs.all()) == 2


def test_outcome_conditioned_agreement_and_abstention_vindicated():
    ivs = InMemoryInterventionStore(); outs = InMemoryOutcomeStore()

    # opp Agree: runtime says contact, human contacts, outcome positive
    run_shadow_step(_opp("Agree", contact=0.9), ivs, human_action="contact", policy_version="p1",
                    proposed_at=10.0, human_at=20.0)
    derive_and_persist(_reply("Agree", 3600.0, "positive_reply"), intervention_store=ivs, outcome_store=outs)

    # opp Override: runtime recommends WAIT (make contact net-negative), human contacts anyway → unsubscribe
    run_shadow_step(_opp("Override", contact=-0.5, wait=0.1), ivs, human_action="contact",
                    policy_version="p1", proposed_at=10.0, human_at=20.0)
    derive_and_persist(_reply("Override", 3600.0, "unsubscribe"), intervention_store=ivs, outcome_store=outs)

    rep = evaluate_shadow(ivs, outs)
    assert rep.paired == 2 and rep.agreement_rate == 0.5 and rep.override_rate == 0.5
    # the runtime's recommendation tracked the better outcome
    assert rep.mean_reward_when_agreed is not None and rep.mean_reward_when_disagreed is not None
    assert rep.mean_reward_when_agreed > rep.mean_reward_when_disagreed
    # runtime said WAIT, human contacted, it went badly → abstention would have been better
    assert rep.abstention_vindicated == 1


def test_evaluate_ignores_opportunities_without_both_actors():
    ivs = InMemoryInterventionStore(); outs = InMemoryOutcomeStore()
    run_shadow_step(_opp("OnlyRuntime"), ivs, human_action="contact", policy_version="p1",
                    proposed_at=10.0, human_at=20.0)
    # remove the human record to simulate "runtime recommended, human hasn't acted yet"
    ivs.records = [r for r in ivs.records if r.actor_type is not ActorType.HUMAN]
    assert evaluate_shadow(ivs, outs).paired == 0


# ── PG end-to-end (skipped without a database) ───────────────────────────────────────
pytest.importorskip("psycopg")
from agentic_os.observation_store import observation_dsn  # noqa: E402
from agentic_os.outcome_store import PostgresOutcomeStore  # noqa: E402
from agentic_os.intervention_record import PostgresInterventionStore  # noqa: E402


@pytest.fixture()
def pg():
    if not observation_dsn():
        pytest.skip("set OBS_DATABASE_URL for the PG shadow-evaluation end-to-end test")
    try:
        ivs = PostgresInterventionStore(); outs = PostgresOutcomeStore()
    except Exception as e:
        pytest.skip(f"Postgres not reachable: {e}")
    yield ivs, outs
    ivs.close(); outs.close()


def test_pg_shadow_step_outcome_and_eval(pg):
    ivs, outs = pg
    subj = f"acct-{uuid.uuid4().hex[:8]}"
    run_shadow_step(_opp(subj, contact=-0.5, wait=0.1), ivs, human_action="contact", policy_version="p1",
                    proposed_at=10.0, human_at=20.0)
    derive_and_persist(_reply(subj, 3600.0, "unsubscribe"), intervention_store=ivs, outcome_store=outs)
    mine = [o for o in evaluate_shadow(ivs, outs).outcomes if o.opportunity_id == f"outreach:{subj}"]
    assert len(mine) == 1
    o = mine[0]
    assert o.human_action == "contact" and o.runtime_action in ("wait", "do nothing")
    assert o.reward is not None and o.reward < 0            # the human's override went badly
