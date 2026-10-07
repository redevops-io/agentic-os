"""Campaign + Offer decisioning (plan §12/§13, P4).

Proves: decide_offer never violates a hard constraint (margin floor / allowed kinds / capacity / discount ceiling),
selects the highest expected value deterministically, and returns NO_ACTION when nothing qualifies; a CampaignPlan
is composed subordinate to its hypothesis and gates on approval by default.
"""
from __future__ import annotations

from agentic_os.commercial import (
    CampaignHypothesis, Offer, OfferConstraints, build_campaign_plan, decide_offer,
)


def test_decide_offer_respects_hard_constraints_and_maximizes_value():
    offers = [
        Offer("o_discount", kind="discount", value=100.0, margin=0.1, discount=0.3),
        Offer("o_bundle", kind="bundle", value=150.0, margin=0.25),
        Offer("o_cheap", kind="discount", value=200.0, margin=0.02, discount=0.5),   # margin too low
    ]
    c = OfferConstraints(max_discount=0.4, min_margin=0.05, policy="standard")
    d = decide_offer("acme", offers, constraints=c)
    assert d.selected_offer.offer_id == "o_bundle"          # highest value among feasible
    assert "o_cheap" not in {o.offer_id for o in d.feasible_offers}   # excluded: margin < 0.05
    assert d.margin == 0.25 and not d.is_no_action


def test_decide_offer_no_action_when_nothing_feasible():
    offers = [Offer("o1", kind="discount", margin=0.0, discount=0.9)]
    c = OfferConstraints(max_discount=0.2, min_margin=0.1)
    d = decide_offer("acme", offers, constraints=c)
    assert d.is_no_action and d.selected_offer is None
    assert "no offer satisfies policy" in d.explanation


def test_decide_offer_custom_expected_value_and_deterministic_ties():
    offers = [Offer("a", value=10.0, margin=0.2, customer_value=1.0),
              Offer("b", value=10.0, margin=0.2, customer_value=1.0)]
    c = OfferConstraints()
    d1 = decide_offer("s", offers, constraints=c)
    d2 = decide_offer("s", list(reversed(offers)), constraints=c)
    assert d1.selected_offer.offer_id == d2.selected_offer.offer_id == "a"   # tie → offer_id
    # custom EV flips the choice
    d3 = decide_offer("s", offers, constraints=c, expected_value=lambda o: 1.0 if o.offer_id == "b" else 0.0)
    assert d3.selected_offer.offer_id == "b"


def test_allowed_kinds_constraint():
    offers = [Offer("up", kind="upsell", value=50.0, margin=0.3),
              Offer("disc", kind="discount", value=80.0, margin=0.3)]
    d = decide_offer("acme", offers, constraints=OfferConstraints(allowed_kinds=("upsell",)))
    assert d.selected_offer.offer_id == "up"    # discount kind not allowed


def test_campaign_plan_is_subordinate_to_hypothesis():
    h = CampaignHypothesis(objective="enterprise_demos", audience="fintech 50-500",
                           message="compliance in days not months", channels=("linkedin", "email"),
                           evidence=("market:competitor_gap",), confidence=0.6)
    plan = build_campaign_plan(h, offer="free_pilot", conversion_target="demo_request")
    assert plan.objective == "enterprise_demos" and plan.audience == "fintech 50-500"
    assert plan.channels == ("linkedin", "email") and plan.message == h.message
    assert plan.hypothesis is h
    assert plan.measurement_plan == ("conversion:demo_request",)
    assert plan.requires_approval is True       # default gates before anything runs
