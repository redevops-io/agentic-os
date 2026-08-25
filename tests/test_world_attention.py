"""Founder Attention Queue — the Business-OS home. Aggregates the governed decisions across every world into
one prioritized, evidence-grounded, dollar-weighted queue. Offline; deterministic."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_contracts import AuthorityContext, PrincipalRef  # noqa: E402
from agentic_os.world import (  # noqa: E402
    ALL_WORLDS,
    AttentionItem,
    AttentionKind,
    Autonomy,
    build_attention_queue,
    items_from_run,
    summarize,
)

_SCOPES = ("read:crm", "read:geo", "write:quote", "write:crm", "read:secrets", "write:vendor", "write:billing")
_SEEDS = {"after-hours-lead": "8842", "kyc-ownership": "clean", "finance-leakage": "4471",
          "gtm-pilot-discovery": "c1", "creator-sponsorship": "s1", "sponsorship-booking": "s1"}


def _auth():
    return AuthorityContext(authority_id="c", principal=PrincipalRef(id="f", tenant="rd"),
                            purpose="home", scope=_SCOPES)


def _queue():
    return build_attention_queue(ALL_WORLDS, authority=_auth(), seeds=_SEEDS, offline=True)


def test_home_aggregates_decisions_across_every_business_system():
    q = _queue()
    assert q, "the home should surface at least one decision"
    worlds = {it.world_id for it in q}
    assert "sponsorship-booking" in worlds and "finance-leakage" in worlds   # spans Revenue + Finance
    blocks = {it.business_block for it in q}
    assert len(blocks) >= 2                                                   # grouped by business system


def test_queue_is_prioritized_approvals_first_then_by_dollars():
    q = _queue()
    ranks = [it.priority() for it in q]
    assert ranks == sorted(ranks)                                            # already prioritized
    approvals = [it for it in q if it.kind == AttentionKind.APPROVAL]
    dollars = [it.dollar_impact for it in approvals]
    assert dollars == sorted(dollars, reverse=True)                          # biggest money first within a kind
    # the $22.5k sponsorship approval outranks the $500 finance approval
    assert approvals[0].world_id == "sponsorship-booking"


def test_every_item_carries_why_block_dollars_realism_and_autonomy():
    for it in _queue():
        assert it.why and it.business_block and it.suggested_action
        assert it.autonomy in (Autonomy.OBSERVE, Autonomy.RECOMMEND, Autonomy.APPROVE, Autonomy.AUTONOMOUS)
        assert it.kind in vars(AttentionKind).values()


def test_summary_counts_and_dollars_awaiting_decision():
    s = summarize(_queue())
    assert s["total"] == sum(s["by_kind"].values())
    assert s["dollars_awaiting_decision"] > 0                                 # money is waiting on the founder
    assert "APPROVAL" in s["by_kind"]


def test_a_safely_stopped_mission_becomes_a_blocked_item():
    # a world with an empty-scope authority is denied its write capability -> safe stop -> BLOCKED on the home
    denied = AuthorityContext(authority_id="c", principal=PrincipalRef(id="f", tenant="rd"),
                              purpose="home", scope=())
    q = build_attention_queue({"finance-leakage": ALL_WORLDS["finance-leakage"]}, authority=denied,
                              seeds=_SEEDS, offline=True)
    assert any(it.kind == AttentionKind.BLOCKED for it in q)


def test_approval_items_are_on_the_approve_rung_of_the_autonomy_ladder():
    for it in _queue():
        if it.kind == AttentionKind.APPROVAL:
            assert it.autonomy == Autonomy.APPROVE
