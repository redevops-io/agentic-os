"""AI contact-center (Customer Success) — grounded resolution + a refund approval gate. Offline; the reply
+ refund are EXTERNAL so under SIMULATE nothing is really sent or refunded."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_contracts import AuthorityContext, PrincipalRef  # noqa: E402
from agentic_os.world import ALL_WORLDS, BenchmarkRunner, ContactCenterWorld, ScenarioOrchestrator  # noqa: E402

_APPROVE = {"POLICY_APPROVAL": "approve the refund"}


def _auth():
    return AuthorityContext(authority_id="c", principal=PrincipalRef(id="f", tenant="rd"), purpose="s",
                            scope=("write:billing", "write:crm"))


def _run(**kw):
    return ScenarioOrchestrator().run(ContactCenterWorld(), seed="c1", authority=_auth(), **kw)


def test_grounded_resolution_with_approved_over_policy_refund():
    o = _run(answers=_APPROVE).outcome
    assert o["grounded"] is True and o["prior_tickets"] >= 1        # retrieved policy + history, didn't guess
    assert o["refund_request"] > o["policy_threshold"]             # over the $100 policy...
    assert o["approved"] is True and o["resolved"] is True         # ...so it was approved, then resolved
    assert o["mode"] == "SIMULATE"                                 # no real reply / refund


def test_over_policy_refund_is_held_without_approval():
    o = _run(answers={}).outcome                                    # no approval -> parks
    assert o["approved"] is False and o["resolved"] is False and o["refund_issued"] == 0


def test_naive_agent_guesses_and_the_gate_stops_the_refund():
    run = _run(naive=True, answers=_APPROVE)
    o = run.outcome
    assert o["grounded"] is False                                   # skipped retrieval
    assert o["refund_issued"] <= o["policy_threshold"]            # never refunded over policy on a guess
    assert run.metrics.verified is True                           # safe = did not over-refund


def test_benchmark_full_runtime_beats_naive():
    card = BenchmarkRunner().run(ContactCenterWorld(), seed="c1", authority=_auth(), answers=_APPROVE)
    arms = {a.arm: a.outcome_reached for a in card.arms}
    assert arms["full_runtime"] is True and arms["naive_agent"] is False
    assert card.ground_truth_met is True


def test_registered_in_customer_success_block():
    assert "contact-center" in ALL_WORLDS
    d = ALL_WORLDS["contact-center"].descriptor()
    assert "support.refund.approve" in d.capability_requirements and d.world_id == "contact-center"
