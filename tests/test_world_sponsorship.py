"""Creator sponsorship world (Phase 1) — discovery + VenueFit + estimate portfolio, no outreach/spend."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_contracts import AuthorityContext, PrincipalRef  # noqa: E402
from agentic_os.world import ALL_WORLDS, CreatorAcquisitionWorld, ScenarioOrchestrator  # noqa: E402


def _run():
    auth = AuthorityContext(authority_id="c", principal=PrincipalRef(id="f", tenant="rd"), purpose="s", scope=())
    return ScenarioOrchestrator().run(CreatorAcquisitionWorld(), seed="s1", authority=auth, offline=True,
                                      answers={"Approve sponsorship portfolio?": "yes"})


def test_produces_portfolio_within_budget_no_spend():
    o = _run().outcome
    assert o["portfolio"] and o["planned_spend"] <= o["budget"]
    assert o["booked"] is False and o["spend_committed"] is False


def test_every_price_is_labelled_an_estimate_not_a_quote():
    for p in _run().outcome["portfolio"]:
        assert p["price_basis"] == "ESTIMATE"          # Phase 1 has no direct quotes; never presented as a quote


def test_ranks_by_buyer_density_not_popularity():
    o = _run().outcome
    titles = [p["venue"] for p in o["portfolio"]]
    # the huge general-AI-news podcast (250k audience, low buyer density) is excluded
    assert "This Week in AI News" not in titles
    assert any("Agents in Production" in t for t in titles)


def test_terminal_action_requests_approval_only():
    run = _run()
    labels = [m.label for m in run.trace.milestones]
    assert any("request founder approval" in l for l in labels)
    assert any(n["reason"] == "POLICY_APPROVAL" for n in run.needs_you)
    # no capability with an EXTERNAL side effect / booking fired
    assert not any("book" in i.capability_id or "commit" in i.capability_id
                   for i in run.trace.__dict__.get("_invocations", []) or [])


def test_registered_and_ground_truth():
    assert "creator-sponsorship" in ALL_WORLDS
    assert _run().metrics.verified is True             # world ground-truth check passed through orchestrator
    d = ALL_WORLDS["creator-sponsorship"].descriptor()
    assert d.realism == "REAL-LIVE" and "iTunes podcast search" in d.datasources
