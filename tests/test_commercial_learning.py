"""One CommercialOutcome loop across all capabilities (plan §28/§10, P10)."""
from __future__ import annotations

from agentic_os.commercial import CommercialOutcome, learn, learn_many
from agentic_os.overlays import LocalRewardLog


def test_every_capability_feeds_one_sink():
    sink = LocalRewardLog()
    learn("acquisition.funnel_optimize", "redevops.io", action="CHANGE_CTA", economic_value=1200.0,
          observed_outcome="conv +0.8pp", policy_version="learned/v3", sink=sink)
    learn("acquisition.offer_decide", "acme", action="select_offer", economic_value=500.0, sink=sink)
    learn("finance.receivables", "inv_7", action="collect", economic_value=300.0, confidence=0.6, sink=sink)
    assert len(sink.outcomes) == 3
    # each reward envelope is tagged with its capability + carries reward + policy_version
    caps = {o.context["capability"] for o in sink.outcomes}
    assert caps == {"acquisition.funnel_optimize", "acquisition.offer_decide", "finance.receivables"}
    funnel = next(o for o in sink.outcomes if o.context["capability"] == "acquisition.funnel_optimize")
    assert funnel.reward == 1200.0 and funnel.policy_version == "learned/v3"


def test_confidence_is_carried_not_inflated():
    sink = LocalRewardLog()
    o = learn("sales.deal_close", "acme", economic_value=0.0, reward=1.0, confidence=0.4, sink=sink)
    assert isinstance(o, CommercialOutcome) and o.confidence == 0.4
    assert sink.outcomes[0].context["confidence"] == 0.4


def test_learn_many_batches():
    sink = LocalRewardLog()
    n = learn_many([CommercialOutcome(capability="operations.order_execute", subject="o1", economic_value=10.0),
                    CommercialOutcome(capability="operations.service_plan", subject="s1", economic_value=20.0)],
                   sink=sink)
    assert n == 2 and len(sink.outcomes) == 2
