"""SR-04 round 4: hook-computed relevance is an UNVALIDATED quality signal (susceptible to keyword stuffing), so every
signal that depends on it carries a documented confidence cap and an explicit evidence line, and can never become an
automatic / low-risk / pre-approved action. Confidence for legacy and supplied relevance is unchanged."""
from __future__ import annotations

import json
import pathlib

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.content.interventions import content_intervention_queue, plan_content_interventions
from agentic_os.content.search_signals import (
    HOOK_CONFIDENCE_CAP, RELEVANCE_UNKNOWN_AWARE_ENV, UNVALIDATED_EVIDENCE, SearchObservation, SignalType,
    cannibalization, ctr_opportunity, emergent_intent, missing_page, near_win)
from agentic_os.integrations.gsc import search_signals_from_observations
from agentic_os.priority_engine import Action, PriorityPolicy

GOLDEN = json.loads((pathlib.Path(__file__).resolve().parent / "golden" / "search_relevance_golden_base.json").read_text())
LINE = "relevance unvalidated: susceptible to keyword stuffing; human review required; not for automatic publication"


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(RELEVANCE_UNKNOWN_AWARE_ENV, raising=False)


def hooked(row, value=None):
    d = dict(row, relevance_source="embedding:fake@r1", relevance_detail="embedder=fake; revision=r1")
    if value is not None:
        d["relevance"] = value
    return SearchObservation(**d)


def test_the_cap_and_the_evidence_line_are_documented_constants():
    assert HOOK_CONFIDENCE_CAP == 0.5 and UNVALIDATED_EVIDENCE == LINE


def test_every_relevance_dependent_signal_from_the_hook_is_capped_and_says_it_is_unvalidated():
    n = 0
    for row in GOLDEN["observations"]:
        for value in (0.62, 0.95, 1.0):
            o = hooked(row, value)
            for s in (near_win(o), missing_page(o), emergent_intent(o, baseline_impressions=0)):
                if s is None:
                    continue
                n += 1
                assert s.confidence <= HOOK_CONFIDENCE_CAP, (s.signal_type, s.confidence)
                assert LINE in s.evidence and any(e.startswith("relevance provenance: embedding:fake@r1") for e in s.evidence)
    assert n > 300


def test_signals_that_do_not_depend_on_relevance_are_not_capped_or_labelled():
    for row in GOLDEN["observations"]:
        base = ctr_opportunity(SearchObservation(**row))
        got = ctr_opportunity(hooked(row))
        assert (base is None) == (got is None)
        if got is not None:
            assert got.confidence == base.confidence and LINE not in got.evidence
    objs = [hooked(r) for r in GOLDEN["observations"][:80]]
    for s in cannibalization(objs):
        assert LINE not in s.evidence and s.confidence == round(min(0.85, 0.5 + 0.1 * len(s.affected_pages)), 3)


def test_legacy_and_supplied_relevance_confidence_is_unchanged():
    for i, row in enumerate(GOLDEN["observations"]):
        base = GOLDEN["per_observation"][i]
        for src in ("unset", "supplied:crm"):
            o = SearchObservation(**dict(row, relevance_source=src))
            for key, s in (("near_win", near_win(o)), ("missing_nocanon", missing_page(o)),
                           ("emergent0", emergent_intent(o, baseline_impressions=0))):
                if base[key] is None:
                    assert s is None
                else:
                    assert s.confidence == json.loads(json.dumps(base[key]))["c"] and LINE not in s.evidence


def test_the_cap_only_ever_lowers_confidence():
    for row in GOLDEN["observations"]:
        for value in (0.62, 0.95):
            uncapped = near_win(SearchObservation(**dict(row, relevance=value, relevance_source="supplied:x")))
            capped = near_win(hooked(row, value))
            if uncapped is not None:
                assert capped.confidence == min(uncapped.confidence, HOOK_CONFIDENCE_CAP)


# ── never auto / low-risk / pre-approved ─────────────────────────────────────────────────────────────────
def _hook_signals():
    rows = [dict(query=f"agent memory {i}", page_url="https://x.io/blog/post-%d" % i, impressions=300, ctr=0.04,
                 position=8.0, relevance=0.95, days_observed=28, site_id="s") for i in range(6)]
    obs = [hooked(r) for r in rows] + [hooked(dict(rows[0], page_url="https://x.io/other"))]
    sigs = [s for o in obs for s in (near_win(o), missing_page(o)) if s is not None]
    assert {s.signal_type for s in sigs} == {SignalType.NEAR_WIN, SignalType.MISSING_PAGE}
    assert all(s.status == "PROPOSE" for s in sigs)
    return sigs


def test_hook_derived_proposals_abstain_under_the_default_policy():
    sigs = _hook_signals()
    q = content_intervention_queue(sigs)
    assert q["count"] == 0 and plan_content_interventions(sigs) == []          # cap 0.5 < default min_confidence 0.55


@pytest.mark.parametrize("policy", [
    PriorityPolicy(min_confidence=0.0),
    PriorityPolicy(min_confidence=0.0, allow_auto_bounded_writes=True, auto_execute_max_tier=RiskTier.CRITICAL),
    PriorityPolicy(min_confidence=0.0, require_positive_value=False, attention_budget=1000)])
def test_even_when_an_operator_lowers_the_bar_hook_derived_signals_are_never_auto_or_pre_approved(policy):
    sigs = _hook_signals()
    decisions = plan_content_interventions(sigs, policy=policy)
    assert decisions
    for d in decisions:
        assert d.action is Action.REQUEST_APPROVAL and d.requires_approval is True
        assert d.candidate.risk_tier is RiskTier.CONSEQUENTIAL
        assert LINE in d.candidate.observation_refs                          # the label travels with the candidate
    q = content_intervention_queue(sigs, policy=policy)
    assert q["auto"] == 0 and q["requires_approval"] == q["count"] > 0
    assert all(v["requires_approval"] for v in q["decisions"])


def test_the_content_candidates_are_consequential_for_every_signal_type():
    # nothing in the interventions module can mark a content candidate low-risk: the tier is fixed
    import inspect
    from agentic_os.content import interventions
    assert "RiskTier.CONSEQUENTIAL" in inspect.getsource(interventions.from_content_signal)
    assert "AUTO" not in inspect.getsource(interventions.from_content_signal)


def test_aggregated_signals_from_the_hook_carry_the_cap_too():
    rows = [hooked(dict(query="agent memory", page_url="https://x.io/blog/p", impressions=300, ctr=0.04, position=8.0,
                        relevance=0.95, days_observed=28, site_id="s"))]
    for s in search_signals_from_observations(rows):
        if s.signal_type is SignalType.NEAR_WIN:
            assert s.confidence <= HOOK_CONFIDENCE_CAP and LINE in s.evidence
