"""Automated outreach — quality gate, persona templates, governed decision — and the cross-channel optimizer."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentic_os.world import (  # noqa: E402
    CandidateAction,
    CrossChannelOptimizer,
    OutreachContext,
    OutreachDecision,
    decide,
    direct_email_action,
    quality_gate,
    render_email,
    select_template,
    send_outreach,
    sponsorship_action,
)


def _grounded(**kw):
    base = dict(company="Acme", first_name="Jordan", role="Head of AI",
                observed_activity="hiring around agent sandboxing and permission scoping",
                specific_evidence_sentence="Your careers page lists an agent-infrastructure role covering "
                                           "sandboxing, permission scoping and AI code review.",
                runtime_problem="whether execution policy and context are implemented independently in each system",
                evidence_about_company="the agent-infrastructure hiring", specific_problem_or_workflow="autonomous coding")
    base.update(kw)
    return OutreachContext(**base)


def test_quality_gate_blocks_without_cited_evidence():
    ok, _ = quality_gate(_grounded())
    assert ok
    bad = _grounded(specific_evidence_sentence="")
    assert quality_gate(bad)[0] is False
    generic = _grounded(specific_evidence_sentence="saw your company and thought you might be interested here")
    assert quality_gate(generic)[0] is False


def test_persona_selects_template():
    assert "× ReDevOps" in select_template("Co-founder & CTO")     # founder-to-founder
    assert "A question about your AI stack" in select_template("Head of AI Platform")  # primary


def test_render_fills_evidence_and_adds_compliance():
    e = render_email(_grounded(role="CTO"))
    assert "Acme" in e["subject"]
    assert "sandboxing, permission scoping" in e["body"]           # evidence middle rendered
    assert "unsubscribe@redevops.io" in e["body"] and "redevops.io" in e["body"]   # compliance footer


def test_decision_ladder():
    # no evidence -> NEEDS_MORE_EVIDENCE
    assert decide(_grounded(specific_evidence_sentence="")) is OutreachDecision.NEEDS_MORE_EVIDENCE
    # gate ok but no verified email -> NO_EMAIL (the real blocker for GitHub/HN-discovered prospects)
    assert decide(_grounded(verified_email="")) is OutreachDecision.NO_EMAIL
    # gate ok + email, auto off -> APPROVAL_REQUIRED
    assert decide(_grounded(verified_email="a@b.com"), auto_send=False) is OutreachDecision.APPROVAL_REQUIRED
    # gate ok + email + auto -> SEND
    assert decide(_grounded(verified_email="a@b.com"), auto_send=True) is OutreachDecision.SEND
    # suppression wins
    assert decide(_grounded(verified_email="a@b.com", suppressed=True)) is OutreachDecision.SUPPRESSED


def test_send_only_fires_on_send_decision_with_cap():
    sent = {"n": 0}

    class FakePM:
        def send(self, *, to, subject, body, tag):
            sent["n"] += 1
            return {"error_code": 0, "message_id": "m1"}

    # SEND + cap -> sends
    r = send_outreach(_grounded(verified_email="a@b.com"), FakePM(), cap_remaining=5, auto_send=True)
    assert r["decision"] == "SEND" and r["sent"] is True and sent["n"] == 1
    # NO_EMAIL -> never sends
    r2 = send_outreach(_grounded(verified_email=""), FakePM(), cap_remaining=5, auto_send=True)
    assert r2["decision"] == "NO_EMAIL" and r2["sent"] is False and sent["n"] == 1
    # cap exhausted -> decided SEND but does not fire
    r3 = send_outreach(_grounded(verified_email="a@b.com"), FakePM(), cap_remaining=0, auto_send=True)
    assert r3["sent"] is False and sent["n"] == 1


def test_cross_channel_optimizer_ranks_by_value_per_cost():
    opt = CrossChannelOptimizer(budget_usd=3000, founder_minutes=120)
    email = direct_email_action(opportunity={"account": "acme", "runtime_fit": 0.9}, has_email=True)
    good_pod = sponsorship_action(placement={"venue": "AI Agents Pod", "price": 546, "expected_qualified_reach": 15000})
    pricey_pod = sponsorship_action(placement={"venue": "Big Consumer Show", "price": 7500, "expected_qualified_reach": 2000})
    wait = CandidateAction(channel="wait", cost_usd=0, founder_minutes=0, expected_qualified_reach=0,
                           expected_pilot_probability=0, expected_pilot_value=0)
    alloc = opt.allocate([email, good_pod, pricey_pod, wait])
    chosen = [c.channel for c in alloc.chosen]
    assert "direct_email" in chosen                                # cheapest value/$ wins a slot
    assert alloc.spent <= 3000 and "Big Consumer Show" not in [c.ref for c in alloc.chosen]  # over budget → rejected


def test_optimizer_waits_when_nothing_clears():
    opt = CrossChannelOptimizer(budget_usd=1, founder_minutes=0)
    wait = CandidateAction(channel="wait", cost_usd=0, founder_minutes=0, expected_qualified_reach=0,
                           expected_pilot_probability=0, expected_pilot_value=0)
    pricey = sponsorship_action(placement={"venue": "X", "price": 5000, "expected_qualified_reach": 1000})
    assert [c.channel for c in opt.allocate([pricey, wait]).chosen] == ["wait"]
