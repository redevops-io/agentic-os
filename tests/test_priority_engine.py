"""Tests for the Priority Engine — the shared proactive-intelligence decision spine.

These assert the two load-bearing invariants the plan insists on (confidence ≠ expected value §18;
do-nothing is a first-class candidate that prevents over-acting §17), the governance routing by risk
tier (§19), the cross-app attention-budget surface (§2), and that the adapters faithfully turn the
already-shipped Growth/Support signals into candidates (and respect those signals' own abstentions).
All deterministic — no live data, no model.
"""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway.contracts import ApprovalPolicy, RiskTier
from agentic_os.priority_engine import (
    Action, AttentionSummary, InterventionCandidate, OutcomeEvent, PriorityPolicy,
    decide, do_nothing, from_support_thread, from_trend_report, priority_score,
    record_outcome, what_needs_me)


def _cand(**kw) -> InterventionCandidate:
    base = dict(source_app="CRM", subject="Acme", proposed_action="send proposal",
                expected_value=0.8, confidence=0.8, risk_tier=RiskTier.READ)
    base.update(kw)
    return InterventionCandidate(**base)


# ── §18: confidence is not expected value ────────────────────────────────────────────
def test_high_impact_moderate_confidence_outranks_certain_low_value():
    certain_trivial = _cand(subject="tiny", expected_value=0.05, confidence=0.99, urgency=0.1)
    likely_big = _cand(subject="big", expected_value=0.9, confidence=0.7, urgency=0.6)
    assert priority_score(likely_big).total > priority_score(certain_trivial).total


def test_urgency_raises_priority_but_does_not_flip_a_negative_value():
    calm = _cand(expected_value=0.6, confidence=0.8, urgency=0.1)
    urgent = _cand(expected_value=0.6, confidence=0.8, urgency=0.9)
    assert priority_score(urgent).total > priority_score(calm).total
    downside = _cand(expected_value=-0.6, confidence=0.8, urgency=0.9)
    assert priority_score(downside).total < 0          # urgency can't rescue a net-negative action


def test_risk_penalty_softened_by_reversibility():
    irreversible = _cand(risk_tier=RiskTier.CRITICAL, reversibility=0.0)
    reversible = _cand(risk_tier=RiskTier.CRITICAL, reversibility=1.0)
    assert priority_score(reversible).total > priority_score(irreversible).total


def test_score_components_are_explainable():
    s = priority_score(_cand(expected_value=0.8, confidence=0.75, urgency=0.5,
                             execution_cost=0.2, attention_cost=0.3, risk_tier=RiskTier.BOUNDED_WRITE))
    assert set(s.components) == {"risk_adjusted_value", "urgency_boost", "information_value",
                                 "execution_cost", "attention_cost", "risk_penalty"}
    assert s.risk_adjusted_value == pytest.approx(0.75 * 0.8)


# ── §17: do-nothing is first-class; abstain rather than over-act ─────────────────────
def test_do_nothing_baseline_is_zero_value():
    dn = do_nothing("Acme")
    assert dn.expected_value == 0.0 and priority_score(dn).total == 0.0


def test_abstains_when_confidence_below_threshold():
    d = decide(_cand(confidence=0.3), PriorityPolicy(min_confidence=0.55))
    assert d.action == Action.ABSTAIN and "confidence" in d.rationale


def test_abstains_when_value_does_not_beat_doing_nothing():
    # a confident but net-negative (or zero) action must not fire
    d = decide(_cand(expected_value=-0.4, confidence=0.9))
    assert d.action == Action.ABSTAIN
    d0 = decide(_cand(expected_value=0.0, confidence=0.9))
    assert d0.action == Action.ABSTAIN


def test_confident_positive_low_risk_acts_automatically():
    d = decide(_cand(expected_value=0.7, confidence=0.9, risk_tier=RiskTier.READ))
    assert d.action == Action.ACT and not d.requires_approval


# ── §19: governance routing by risk tier ─────────────────────────────────────────────
def test_consequential_and_critical_require_approval():
    for tier in (RiskTier.CONSEQUENTIAL, RiskTier.CRITICAL):
        d = decide(_cand(expected_value=0.8, confidence=0.9, risk_tier=tier))
        assert d.action == Action.REQUEST_APPROVAL and d.requires_approval


def test_bounded_write_needs_approval_unless_deployment_opts_in():
    c = _cand(expected_value=0.8, confidence=0.9, risk_tier=RiskTier.BOUNDED_WRITE)
    assert decide(c, PriorityPolicy()).action == Action.REQUEST_APPROVAL
    opted_in = PriorityPolicy(allow_auto_bounded_writes=True)
    assert decide(c, opted_in).action == Action.ACT


def test_explicit_approval_policy_overrides_tier_default():
    c = _cand(expected_value=0.8, confidence=0.9, risk_tier=RiskTier.READ,
              approval_policy=ApprovalPolicy.MANDATORY)
    assert decide(c).action == Action.REQUEST_APPROVAL


# ── §2: the cross-app "what needs me?" surface ───────────────────────────────────────
def _mixed_candidates():
    return [
        _cand(source_app="CRM", subject="Acme", expected_value=0.9, confidence=0.86, urgency=0.7,
              risk_tier=RiskTier.CONSEQUENTIAL, candidate_id="crm:acme"),
        _cand(source_app="Projects", subject="pilot", expected_value=0.8, confidence=0.84, urgency=0.9,
              risk_tier=RiskTier.CONSEQUENTIAL, candidate_id="proj:pilot"),
        _cand(source_app="Growth", subject="topic", expected_value=0.6, confidence=0.78, urgency=0.85,
              risk_tier=RiskTier.CONSEQUENTIAL, candidate_id="growth:x"),
        _cand(source_app="Knowledge", subject="stale doc", expected_value=0.4, confidence=0.7,
              risk_tier=RiskTier.READ, candidate_id="kb:stale"),                     # auto
        _cand(source_app="CRM", subject="Maybe", expected_value=0.5, confidence=0.3,
              risk_tier=RiskTier.CONSEQUENTIAL, candidate_id="crm:maybe"),           # abstain (weak)
    ]


def test_attention_budget_surfaces_top_k_defers_the_rest():
    s = what_needs_me(_mixed_candidates(), PriorityPolicy(attention_budget=2))
    assert isinstance(s, AttentionSummary)
    assert len(s.surfaced) == 2                              # only the budget's worth interrupts me
    assert all(d.action == Action.REQUEST_APPROVAL for d in s.surfaced)
    # surfaced are the highest priority; the third approval-needing item is deferred, not dropped
    assert len(s.deferred) == 1 and s.deferred[0].action == Action.DEFER
    assert len(s.handled_automatically) == 1                 # the READ-tier KB draft ran automatically
    assert s.abstained == 1                                  # the weak lead
    assert s.surfaced[0].priority.total >= s.surfaced[1].priority.total


def test_surface_renders_the_plan_shaped_summary():
    s = what_needs_me(_mixed_candidates(), PriorityPolicy(attention_budget=3))
    text = s.render()
    assert "need" in text.lower() and "you today" in text.lower()
    assert "handled automatically, deferred, or judged too low-value" in text


def test_expired_candidates_drop_out():
    c = _cand(expected_value=0.9, confidence=0.9, risk_tier=RiskTier.CONSEQUENTIAL, expiry=100.0)
    s = what_needs_me([c], now=200.0)
    assert not s.surfaced and s.abstained == 1
    s2 = what_needs_me([c], now=50.0)
    assert len(s2.surfaced) == 1


def test_empty_and_all_abstain_surfaces_render_cleanly():
    assert what_needs_me([]).render() == "Nothing needs you right now."
    only_weak = what_needs_me([_cand(confidence=0.1)])
    assert not only_weak.surfaced and "Nothing needs you" in only_weak.render()


# ── §20: outcome telemetry contract ──────────────────────────────────────────────────
def test_record_outcome_captures_the_learning_signal():
    d = decide(_cand(expected_value=0.8, confidence=0.9, risk_tier=RiskTier.CONSEQUENTIAL,
                     candidate_id="crm:acme"))
    ev = record_outcome(d, accepted=True, observed_reward=1.0, note="reply within a day")
    assert isinstance(ev, OutcomeEvent)
    assert ev.candidate_id == "crm:acme" and ev.action == Action.REQUEST_APPROVAL
    assert ev.accepted is True and ev.observed_reward == 1.0


# ── adapters: real shipped signals in, candidates out ────────────────────────────────
def test_trend_adapter_respects_the_kernels_abstention():
    from agentic_os.trend_intelligence import TrendCandidate, assess, Mode
    thin = TrendCandidate(entity="thin", series={"reddit": [1, 2, 3, 4, 5, 6, 7, 8]})  # one source
    report = assess(thin, mode=Mode.STRICT)
    assert report.abstained
    assert from_trend_report(report) is None                 # no candidate from an abstained report


def test_trend_adapter_builds_an_approval_gated_candidate_from_a_strong_report():
    from agentic_os.trend_intelligence import TrendCandidate, assess, Mode
    rising = [1, 1.5, 2.3, 3.6, 5.6, 8.7, 13.5, 21.0]
    strong = TrendCandidate(
        entity="agentic-rag", series={s: list(rising) for s in ("reddit", "youtube", "search")},
        creator_outliers=(8.0, 6.0), question_growth=1.0, geo_count=4)
    report = assess(strong, mode=Mode.EXPLORATORY)
    c = from_trend_report(report)
    assert c is not None and "agentic-rag" in c.subject
    assert c.risk_tier == RiskTier.CONSEQUENTIAL                # publishing is an external action
    assert 0.0 <= c.confidence <= 1.0 and c.expected_value == pytest.approx(report.capitalization_gap)
    assert decide(c).action == Action.REQUEST_APPROVAL


def test_support_adapter_only_fires_when_a_follow_up_is_due():
    from agentic_os.support_autonomy import FollowUpPolicy, ThreadState, qualify_lead
    now = 1_000_000.0
    lead = qualify_lead(message="what's your pricing?", has_email=True, message_count=3)  # P0-ish
    # not yet due → no candidate
    fresh = ThreadState(last_inbound_at=now - 60, status="awaiting_customer")
    pol = FollowUpPolicy(clock=lambda: now)
    assert from_support_thread(fresh, pol.assess(fresh), lead) is None
    # overdue → an approval-gated outbound candidate
    stale = ThreadState(last_inbound_at=now - 4000, status="awaiting_customer")
    d = pol.assess(stale)
    assert d.should_follow_up
    c = from_support_thread(stale, d, lead)
    assert c is not None and c.risk_tier == RiskTier.CONSEQUENTIAL
    assert c.expected_value == pytest.approx(1.0)              # P0 lead ⇒ top value
    assert decide(c).action == Action.REQUEST_APPROVAL


def test_support_adapter_respects_opt_out():
    from agentic_os.support_autonomy import FollowUpPolicy, ThreadState, qualify_lead
    now = 1_000_000.0
    opted = ThreadState(last_inbound_at=now - 4000, status="awaiting_customer", opted_out=True)
    pol = FollowUpPolicy(clock=lambda: now)
    assert from_support_thread(opted, pol.assess(opted), qualify_lead(message="hi")) is None


def test_end_to_end_growth_and_support_into_one_surface():
    from agentic_os.trend_intelligence import TrendCandidate, assess, Mode
    from agentic_os.support_autonomy import FollowUpPolicy, ThreadState, qualify_lead
    now = 1_000_000.0
    rising = [1, 1.5, 2.3, 3.6, 5.6, 8.7, 13.5, 21.0]
    report = assess(TrendCandidate(entity="topic", series={s: list(rising) for s in ("reddit", "youtube", "search")},
                                   creator_outliers=(8.0, 6.0), question_growth=1.0, geo_count=4),
                    mode=Mode.EXPLORATORY)
    stale = ThreadState(last_inbound_at=now - 4000, status="awaiting_customer")
    pol = FollowUpPolicy(clock=lambda: now)
    lead = qualify_lead(message="pricing?", has_email=True, message_count=3)
    candidates = [c for c in (from_trend_report(report),
                              from_support_thread(stale, pol.assess(stale), lead)) if c]
    assert len(candidates) == 2
    s = what_needs_me(candidates, PriorityPolicy(attention_budget=3))
    assert len(s.surfaced) == 2                               # both are approval-gated, both surface
    assert {d.candidate.source_app for d in s.surfaced} == {"growth", "support"}
