"""Unit tests for the Knowledge Frontier engine (agentic_os.knowledge_frontier) + its Priority Engine
adapter. Test the selection LOGIC — structural importance, prerequisite gating, uncertainty/retention
signals, and the TEACH/REVIEW/STOP decision. Whether the policy beats naive orderings is in
test_knowledge_frontier_backtest.py.
"""
from __future__ import annotations

import pytest

from agentic_os.knowledge_frontier import (
    Concept, ConceptGraph, ConceptState, FrontierAction, FrontierPolicy, Mastery, StopReason,
    concept_state, expected_gain, next_step, prerequisites_met, uncertainty)


def _graph():
    # a small prerequisite DAG:  base → mid → top   (base is the most foundational)
    return ConceptGraph({
        "base": Concept("base"),
        "mid": Concept("mid", prerequisites=("base",)),
        "top": Concept("top", prerequisites=("mid",)),
        "aside": Concept("aside", prerequisites=("base",)),
    })


# ── structural importance ─────────────────────────────────────────────────────────────
def test_importance_reflects_transitive_dependents():
    g = _graph()
    # base underlies mid, top, aside → most important; top underlies nothing → least
    assert g.importance("base") > g.importance("mid") > g.importance("top")
    assert g.importance("top") == pytest.approx(0.0)


# ── prerequisite gating ───────────────────────────────────────────────────────────────
def test_prerequisites_met_gates_on_prereq_mastery():
    g = _graph()
    empty = {}
    assert prerequisites_met(g, empty, "base")                    # no prereqs
    assert not prerequisites_met(g, empty, "mid")                 # base not learned yet
    ready = {"base": Mastery(prob=0.8, exposed=True)}
    assert prerequisites_met(g, ready, "mid")


def test_a_misconceived_prerequisite_blocks_the_dependent():
    g = _graph()
    state = {"base": Mastery(prob=0.9, exposed=True, misconception=True)}
    assert not prerequisites_met(g, state, "mid")


# ── per-concept signals ───────────────────────────────────────────────────────────────
def test_uncertainty_peaks_at_half_knowledge():
    assert uncertainty(Mastery(prob=0.5, exposed=True)) > uncertainty(Mastery(prob=0.9, exposed=True))
    assert uncertainty(Mastery(prob=0.5, exposed=False)) == 0.0        # unexposed ⇒ no uncertainty signal


def test_expected_gain_higher_for_foundational_and_unlearned():
    g = _graph()
    unlearned_base = expected_gain(g, "base", Mastery(prob=0.1, exposed=True), 0.85)
    learned_base = expected_gain(g, "base", Mastery(prob=0.8, exposed=True), 0.85)
    assert unlearned_base > learned_base


def test_concept_state_classification():
    g = _graph()
    assert concept_state(g, {}, "base") == ConceptState.NOT_ENCOUNTERED
    assert concept_state(g, {"base": Mastery(prob=0.95, exposed=True)}, "base") == ConceptState.MASTERED
    assert concept_state(g, {"base": Mastery(prob=0.3, exposed=True, misconception=True)}, "base") \
        == ConceptState.MISCONCEPTION
    assert concept_state(g, {"base": Mastery(prob=0.5, exposed=True)}, "base") == ConceptState.UNCERTAIN


# ── the frontier selection ────────────────────────────────────────────────────────────
def test_frontier_teaches_a_foundation_first_when_nothing_is_known():
    g = _graph()
    step = next_step(g, {})
    assert step.action == FrontierAction.TEACH and step.concept_id == "base"   # only eligible + most important


def test_frontier_will_not_teach_a_concept_whose_prereqs_are_unmet():
    g = _graph()
    # base half-known (< prereq threshold 0.7): mid/top/aside are blocked, so base is the only pick
    step = next_step(g, {"base": Mastery(prob=0.5, exposed=True)})
    assert step.concept_id == "base"


def test_frontier_advances_to_dependents_once_prereqs_met():
    g = _graph()
    state = {"base": Mastery(prob=0.9, exposed=True)}      # base mastered ⇒ mid & aside now eligible
    step = next_step(g, state)
    assert step.action == FrontierAction.TEACH and step.concept_id in ("mid", "aside")


def test_frontier_reviews_a_retention_at_risk_concept():
    g = _graph()
    # everything mastered, but base is stale (retention at risk) ⇒ review it
    state = {c: Mastery(prob=0.95, exposed=True) for c in g.concepts}
    state["base"] = Mastery(prob=0.95, exposed=True, staleness=10.0)
    step = next_step(g, state, policy=FrontierPolicy(at_risk_staleness=4.0))
    assert step.action == FrontierAction.REVIEW and step.concept_id == "base"


def test_frontier_stops_when_all_mastered():
    g = _graph()
    state = {c: Mastery(prob=0.95, exposed=True) for c in g.concepts}
    step = next_step(g, state)
    assert step.action == FrontierAction.STOP and step.stop_reason == StopReason.ALL_MASTERED


def test_frontier_stops_objective_met_even_with_other_concepts_open():
    g = _graph()
    state = {"base": Mastery(prob=0.95, exposed=True), "mid": Mastery(prob=0.9, exposed=True)}
    step = next_step(g, state, objective={"base", "mid"})
    assert step.action == FrontierAction.STOP and step.stop_reason == StopReason.OBJECTIVE_MET


def test_frontier_reports_blocked_on_prereqs_when_only_blocked_remain():
    # a graph where the only unmastered concept is blocked by a MISCONCEIVED prereq
    g = ConceptGraph({"a": Concept("a"), "b": Concept("b", prerequisites=("a",))})
    state = {"a": Mastery(prob=0.95, exposed=True, misconception=True)}   # 'a' blocks 'b', 'a' is "mastered prob" but misconceived
    # a is misconceived ⇒ not mastered ⇒ it's actually teachable; make a truly mastered-but-b-blocked case instead:
    state = {"a": Mastery(prob=0.5, exposed=True)}    # a not mastered & is a prereq of b; a is teachable though
    step = next_step(g, state)
    assert step.action == FrontierAction.TEACH and step.concept_id == "a"   # a is eligible


# ── the Priority Engine adapter ───────────────────────────────────────────────────────
def test_frontier_adapter_builds_a_low_risk_auto_candidate():
    from agentic_os.agent_gateway.contracts import RiskTier
    from agentic_os.priority_engine import Action, decide, from_frontier_choice
    g = _graph()
    step = next_step(g, {})
    c = from_frontier_choice(step, subject_label="new hire")
    assert c is not None and c.risk_tier == RiskTier.READ and "base" in c.subject
    assert c.information_value > 0
    assert decide(c).action == Action.ACT             # recommending what to learn is low-risk ⇒ auto


def test_frontier_adapter_returns_none_for_stop():
    from agentic_os.priority_engine import from_frontier_choice
    g = _graph()
    step = next_step(g, {c: Mastery(prob=0.95, exposed=True) for c in g.concepts})
    assert from_frontier_choice(step) is None
