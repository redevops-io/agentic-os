"""Tests for Phase A0 over the operational store (agentic_os.a0_evaluation)."""
from __future__ import annotations

import uuid

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.a0_evaluation import decision_points_from_interventions, run_a0
from agentic_os.historical_replay import DecisionPoint, LeakageError
from agentic_os.intervention_record import (
    InMemoryInterventionStore, record_human_action, select_and_record)
from agentic_os.observation import KnownAtQuality, Observation
from agentic_os.observation_store import InMemoryObservationStore
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate


def _obs(oid, subject, valid_at, known_at, quality=KnownAtQuality.OBSERVED):
    return Observation(oid, "whatsapp_waha", "crm.engaged", subject, valid_at=valid_at, known_at=known_at,
                       ingested_at=known_at, known_at_quality=quality)


def _builder(subject, obs):
    if not any(o.kind == "crm.engaged" for o in obs):    # fires only when as-of evidence shows engagement
        return None
    refs = tuple(o.observation_id for o in obs)
    return DecisionOpportunity(
        entity=subject, source_app="outreach", opportunity_id=f"outreach:{subject}",
        candidate_actions=(
            InterventionCandidate("outreach", subject, "contact", 0.8, 0.85, action_kind="contact",
                                  risk_tier=RiskTier.CONSEQUENTIAL, observation_refs=refs,
                                  candidate_id=f"outreach:{subject}:contact"),
            InterventionCandidate("outreach", subject, "wait", 0.15, 0.7, action_kind="wait",
                                  risk_tier=RiskTier.READ, candidate_id=f"outreach:{subject}:wait")))


def test_a0_over_store_scores_agreement_and_flags_broken_projections():
    obs = InMemoryObservationStore()
    obs.append(_obs("e-acme", "Acme", valid_at=10, known_at=10))
    obs.append(_obs("e-beta", "Beta", valid_at=10, known_at=10))
    points = [
        DecisionPoint(decision_time=50, subject="Acme", known_good_action="contact"),   # agree
        DecisionPoint(decision_time=50, subject="Beta", known_good_action="wait"),       # disagree
        DecisionPoint(decision_time=50, subject="Gamma", known_good_action="contact"),   # no evidence
    ]
    rep = run_a0(obs, points, _builder)
    assert rep.n_points == 3 and rep.broken_projections == 1        # Gamma had no as-of evidence
    assert rep.evaluated == 2 and rep.agreement == 1 and rep.agreement_rate == 0.5
    assert rep.gate_violations == 0


def test_a0_reconstruction_hides_future_evidence():
    obs = InMemoryObservationStore()
    obs.append(_obs("late", "Acme", valid_at=10, known_at=999))     # learned only after the decision
    rep = run_a0(obs, [DecisionPoint(50, "Acme", known_good_action="contact")], _builder)
    assert rep.broken_projections == 1 and rep.evaluated == 0       # invisible as-of T=50


def test_a0_excludes_unknown_provenance_fail_closed():
    obs = InMemoryObservationStore()
    obs.append(_obs("snap", "Acme", valid_at=10, known_at=10, quality=KnownAtQuality.UNKNOWN))
    rep = run_a0(obs, [DecisionPoint(50, "Acme", known_good_action="contact")], _builder)
    assert rep.broken_projections == 1                              # UNKNOWN can't enter A0


def test_a0_raises_if_builder_cites_evidence_outside_the_asof_set():
    obs = InMemoryObservationStore()
    obs.append(_obs("e-acme", "Acme", valid_at=10, known_at=10))

    def cheating(subject, observations):
        return DecisionOpportunity(
            entity=subject, source_app="outreach", opportunity_id=f"outreach:{subject}",
            candidate_actions=(InterventionCandidate(
                "outreach", subject, "contact", 0.8, 0.9, action_kind="contact",
                observation_refs=("does-not-exist-in-asof",), candidate_id="x"),))

    with pytest.raises(LeakageError):
        run_a0(obs, [DecisionPoint(50, "Acme")], cheating)


def test_decision_points_derived_from_recorded_history():
    ivs = InMemoryInterventionStore()
    opp = DecisionOpportunity(
        entity="Acme", source_app="outreach", opportunity_id="outreach:Acme",
        candidate_actions=(InterventionCandidate("outreach", "Acme", "contact", 0.9, 0.85,
                                                 action_kind="contact", risk_tier=RiskTier.CONSEQUENTIAL,
                                                 candidate_id="outreach:Acme:contact"),))
    select_and_record(opp, ivs, policy_version="p1", proposed_at=100.0, id_fn=lambda: "r1")
    record_human_action(ivs, opportunity_id="outreach:Acme", action_kind="contact", at=200.0,
                        intervention_id="h1")
    points = decision_points_from_interventions(ivs)
    assert len(points) == 1                                         # one runtime decision
    assert points[0].subject == "Acme" and points[0].decision_time == 100.0
    assert points[0].known_good_action == "contact"                # labelled by the human's later action


# ── PG end-to-end (skipped without a database) ───────────────────────────────────────
pytest.importorskip("psycopg")
from agentic_os.observation_store import PostgresObservationStore, observation_dsn  # noqa: E402
from agentic_os.intervention_record import PostgresInterventionStore  # noqa: E402


@pytest.fixture()
def pg():
    if not observation_dsn():
        pytest.skip("set OBS_DATABASE_URL for the PG A0 end-to-end test")
    try:
        o = PostgresObservationStore(); i = PostgresInterventionStore()
    except Exception as e:
        pytest.skip(f"Postgres not reachable: {e}")
    yield o, i
    o.close(); i.close()


def test_pg_a0_end_to_end(pg):
    obs_store, ivs = pg
    subj = f"acct-{uuid.uuid4().hex[:8]}"
    obs_store.append(_obs(f"e-{uuid.uuid4().hex}", subj, valid_at=10.0, known_at=10.0))
    # runtime decided at T=50 (from real evidence); the human later contacted → the A0 label
    opp = DecisionOpportunity(
        entity=subj, source_app="outreach", opportunity_id=f"outreach:{subj}",
        candidate_actions=(InterventionCandidate("outreach", subj, "contact", 0.9, 0.85,
                                                 action_kind="contact", risk_tier=RiskTier.CONSEQUENTIAL,
                                                 candidate_id=f"outreach:{subj}:contact"),))
    select_and_record(opp, ivs, policy_version="p1", proposed_at=50.0, id_fn=lambda: f"r-{subj}")
    record_human_action(ivs, opportunity_id=f"outreach:{subj}", action_kind="contact", at=100.0,
                        intervention_id=f"h-{subj}")
    points = [p for p in decision_points_from_interventions(ivs) if p.subject == subj]
    rep = run_a0(obs_store, points, _builder)
    assert rep.evaluated == 1 and rep.agreement == 1 and rep.gate_violations == 0
