"""Paid acquisition (Reddit ads) + the cross-channel Budget Governor. Offline; the campaign commit is
EXTERNAL so under SIMULATE nothing is really bought. Proves overspend is 0 even under full approval."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_contracts import AuthorityContext, PrincipalRef  # noqa: E402
from agentic_os.world import (  # noqa: E402
    ALL_WORLDS,
    BudgetGovernor,
    PaidAcquisitionWorld,
    ScenarioOrchestrator,
)

_APPROVE = {"POLICY_APPROVAL": "approve the campaign"}


def _auth():
    return AuthorityContext(authority_id="c", principal=PrincipalRef(id="f", tenant="rd"), purpose="p", scope=())


def _run(**kw):
    return ScenarioOrchestrator().run(PaidAcquisitionWorld(), seed="s1", authority=_auth(), offline=True, **kw)


# ------------------------------------------------------------------------ the Budget Governor (unit)
def test_governor_refuses_over_total_and_over_channel_ceiling():
    g = BudgetGovernor(total_budget=1000.0, channel_ceilings={"reddit": 800.0})
    assert g.commit("reddit", 600.0).allowed and g.remaining() == 400.0
    over_ceiling = g.commit("reddit", 300.0)                      # 600+300 > 800 ceiling
    assert over_ceiling.allowed is False and "ceiling" in over_ceiling.reason
    assert g.commit("reddit", 150.0).allowed and g.remaining() == 250.0
    assert g.commit("reddit", 999.0).allowed is False            # over remaining total
    assert g.committed_total() == 750.0                          # refused commits changed nothing


def test_governor_rejects_non_positive():
    g = BudgetGovernor(total_budget=100.0)
    assert g.authorize("x", 0).allowed is False and g.authorize("x", -5).allowed is False


# ------------------------------------------------------------------------ the world (integration)
def test_campaign_commits_within_budget_and_governor_refuses_the_overrun():
    o = _run(answers=_APPROVE).outcome
    assert o["approved"] is True and o["committed"]                       # some placements bought (simulated)
    assert o["committed_total"] <= o["budget"]                            # never over budget...
    assert o["refused"]                                                   # ...because the governor refused the tail
    assert any("exceeds remaining budget" in r["reason"] for r in o["refused"])
    assert o["mode"] == "SIMULATE"                                        # no real ad bought


def test_governance_invariants_are_zero_and_ground_truth_passes():
    run = _run(answers=_APPROVE)
    assert run.outcome["governance"] == {"overspend": False, "commit_without_approval": False,
                                         "commit_over_channel_ceiling": False}
    assert run.metrics.verified is True


def test_buyer_density_excludes_the_big_general_community():
    o = _run(answers=_APPROVE).outcome
    assert "r/artificial" in o["excluded"]                               # 900k subs but consumer -> excluded
    assert all(c["id"] != "r/artificial" for c in o["committed"])


def test_naive_guess_spends_nothing():
    o = _run(naive=True, answers=_APPROVE).outcome
    assert o["approved"] is False and not o["committed"]                  # a guess is not an approval
    assert _run(naive=True, answers=_APPROVE).metrics.verified is True   # safely spent nothing


def test_attribution_and_optimizer_present():
    o = _run(answers=_APPROVE).outcome
    assert o["attribution_complete"] is True
    assert o["optimizer"]["chosen"]                                       # cross-channel comparison ran


def test_registered_with_budget_capabilities():
    assert "paid-acquisition" in ALL_WORLDS
    d = ALL_WORLDS["paid-acquisition"].descriptor()
    assert "ads.campaign.commit" in d.capability_requirements and d.world_id == "paid-acquisition"
