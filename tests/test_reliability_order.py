"""Workflow Reliability + Order Execution (plan §16/§17, P6).

Proves: evaluate_transition is correct for occurred/overdue/diverged/pending; and an OrderExecution reuses the
generic reliability engine to detect a missed transition — order is an application, not a bespoke monitor.
"""
from __future__ import annotations

from agentic_os.reliability import (
    ExpectedTransition, FailureType, TransitionStatus, evaluate_transition, monitor_transitions,
)
from agentic_os.commercial import OrderExecution, OrderLine, evaluate_order, order_expected_transitions


def _t(**kw):
    return ExpectedTransition(subject="s", expected_to_state="done", from_state="start",
                              expected_by=100, failure_states=("cancelled",), remediation_policy="fix", **kw)


def test_transition_occurred_no_incident():
    assert evaluate_transition(_t(), observed_state="done", now=200) is None


def test_transition_overdue():
    inc = evaluate_transition(_t(), observed_state="start", now=200)
    assert inc is not None and inc.failure_type is FailureType.NOT_OCCURRED
    assert inc.status is TransitionStatus.OVERDUE and inc.candidate_remediations == ("fix",)


def test_transition_diverged_to_failure_state():
    inc = evaluate_transition(_t(), observed_state="cancelled", now=50)
    assert inc.failure_type is FailureType.DIVERGED and inc.status is TransitionStatus.FAILED


def test_transition_pending_within_window():
    assert evaluate_transition(_t(), observed_state="start", now=50) is None


def test_order_detects_missed_transition():
    order = OrderExecution(order_id="o1", customer="acme",
                           lines=(OrderLine("sku1", quantity=10, unit_price_cents=5000),), required_by=100)
    assert order.value_cents == 50000
    transitions = order_expected_transitions(order)
    assert len(transitions) == 5   # 6 stages → 5 transitions
    # everything stuck at 'received' past the deadline → every downstream transition overdue
    observed = {t.subject: "received" for t in transitions}
    incidents = evaluate_order(order, observed_stage_states=observed, now=200)
    assert len(incidents) == 5
    assert all(i.status is TransitionStatus.OVERDUE for i in incidents)
    # once fulfilled on time, that transition is healthy
    observed2 = {f"order:o1:sourced": "sourced", f"order:o1:reserved": "reserved",
                 f"order:o1:fulfilled": "fulfilled", f"order:o1:invoiced": "invoiced",
                 f"order:o1:verified": "verified"}
    assert evaluate_order(order, observed_stage_states=observed2, now=200) == ()
