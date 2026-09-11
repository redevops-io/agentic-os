"""Tests for Phase A0 historical replay + the hard leakage gate (agentic_os.historical_replay)."""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.historical_replay import (
    DecisionPoint, LeakageError, ReplayReport, as_of, assert_no_leakage, audit_leakage, is_knowable_at,
    replay)
from agentic_os.observation import Observation
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate


def _obs(oid, subject, valid_at, known_at):
    return Observation(observation_id=oid, source="crm", kind="crm.event", subject=subject,
                       valid_at=valid_at, known_at=known_at)


# ── the leakage gate (§4.1) ───────────────────────────────────────────────────────────
def test_as_of_excludes_future_on_both_time_axes():
    obs = [
        _obs("past", "Acme", valid_at=10, known_at=10),
        _obs("valid-future", "Acme", valid_at=200, known_at=50),    # true only later
        _obs("known-future", "Acme", valid_at=10, known_at=200),    # we learned it only later (backfill)
        _obs("other", "Beta", valid_at=5, known_at=5),
    ]
    kept = as_of(obs, decision_time=100, subject="Acme")
    assert [o.observation_id for o in kept] == ["past"]             # both future kinds excluded


def test_assert_no_leakage_raises_on_a_future_feature():
    clean = [_obs("a", "Acme", 10, 10)]
    assert_no_leakage(clean, decision_time=100)                    # no raise
    leaky = clean + [_obs("late", "Acme", valid_at=10, known_at=150)]
    with pytest.raises(LeakageError):
        assert_no_leakage(leaky, decision_time=100)


def test_audit_leakage_reports_without_raising():
    obs = [_obs("ok", "Acme", 10, 10), _obs("leak", "Acme", 300, 10)]
    bad = audit_leakage(obs, decision_time=100)
    assert [o.observation_id for o in bad] == ["leak"]
    assert is_knowable_at(obs[0], 100) and not is_knowable_at(obs[1], 100)


# ── replay ────────────────────────────────────────────────────────────────────────────
def _builder(subject, obs):
    # a tiny reconstructed opportunity: only fires if there's an as-of "engaged" signal
    engaged = any(o.kind == "crm.engaged" for o in obs)
    if not engaged:
        return None
    refs = tuple(o.observation_id for o in obs)
    return DecisionOpportunity(
        entity=subject, source_app="crm", opportunity_id=f"crm:{subject}",
        candidate_actions=(
            InterventionCandidate("crm", subject, "send proposal", 0.8, 0.85, action_kind="send_proposal",
                                  risk_tier=RiskTier.CONSEQUENTIAL, observation_refs=refs,
                                  candidate_id=f"crm:{subject}:send_proposal"),
            InterventionCandidate("crm", subject, "monitor", 0.15, 0.7, action_kind="monitor",
                                  risk_tier=RiskTier.READ, candidate_id=f"crm:{subject}:monitor")))


def test_replay_scores_agreement_and_flags_broken_projections():
    obs = [
        Observation("e1", "crm", "crm.engaged", "Acme", valid_at=10, known_at=10),
        Observation("e2", "crm", "crm.engaged", "Beta", valid_at=10, known_at=10),
    ]
    points = [
        DecisionPoint(decision_time=50, subject="Acme", known_good_action="send_proposal"),
        DecisionPoint(decision_time=50, subject="Beta", known_good_action="monitor"),
        DecisionPoint(decision_time=50, subject="Gamma", known_good_action="send_proposal"),  # no evidence
    ]
    rep = replay(obs, points, _builder)
    assert isinstance(rep, ReplayReport)
    assert rep.n_points == 3 and rep.broken_projections == 1       # Gamma had no as-of evidence
    assert rep.evaluated == 2 and rep.gate_violations == 0
    assert 0.0 <= rep.agreement_rate <= 1.0


def test_replay_reconstructs_as_of_state_no_future_leaks_in():
    # 'engaged' becomes known only AFTER the decision → at T the opportunity must NOT fire
    obs = [Observation("late", "crm", "crm.engaged", "Acme", valid_at=10, known_at=999)]
    rep = replay(obs, [DecisionPoint(decision_time=50, subject="Acme", known_good_action="send_proposal")],
                 _builder)
    assert rep.broken_projections == 1 and rep.evaluated == 0      # the future signal was invisible as-of T


def test_replay_raises_if_the_builder_cites_future_evidence():
    # a cheating builder that references a future observation id → the gate trips
    future = Observation("future-ref", "crm", "crm.engaged", "Acme", valid_at=10, known_at=999)

    def cheating_builder(subject, obs):
        return DecisionOpportunity(
            entity=subject, source_app="crm", opportunity_id=f"crm:{subject}",
            candidate_actions=(InterventionCandidate(
                "crm", subject, "send", 0.8, 0.9, action_kind="send_proposal",
                observation_refs=("future-ref",), candidate_id="crm:x:send"),))

    with pytest.raises(LeakageError):
        replay([future], [DecisionPoint(decision_time=50, subject="Acme")], cheating_builder)
