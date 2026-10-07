"""Order Execution Intelligence — an application of Workflow Reliability (plan §16, P6).

The audit found order *intelligence* (lineage/blockers) but no execution loop. This models an order as a sequence
of expected transitions (received → sourced → reserved → fulfilled → invoiced → verified) and reuses the generic
``reliability`` engine to detect a missed/overdue/diverged transition — so OrderExecution is "just an application"
of the reusable abstraction, not a bespoke monitor. Pure + deterministic; real sourcing/inventory/logistics reads
and remediation Missions are bound in the enterprise overlay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence, Tuple

from ..reliability import ExpectedTransition, ReliabilityIncident, monitor_transitions

# the canonical order fulfillment chain
ORDER_STAGES: Tuple[str, ...] = ("received", "sourced", "reserved", "fulfilled", "invoiced", "verified")


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int = 0
    unit_price_cents: int = 0


@dataclass(frozen=True)
class OrderExecution:
    """A customer commitment being carried across systems to fulfillment (§16)."""
    order_id: str
    customer: str = ""
    lines: Tuple[OrderLine, ...] = ()
    required_by: int = 0                        # deadline in ms
    current_state: str = "received"
    evidence: Tuple[str, ...] = ()

    @property
    def value_cents(self) -> int:
        return sum(l.quantity * l.unit_price_cents for l in self.lines)


def order_expected_transitions(order: OrderExecution, *, stage_deadlines: Mapping[str, int] = None) -> Tuple[ExpectedTransition, ...]:
    """The chain of expected transitions for an order. Each stage's deadline defaults to the order's required_by;
    ``stage_deadlines`` overrides per stage. ``cancelled`` is a failure state for every stage."""
    deadlines = dict(stage_deadlines or {})
    out = []
    for i in range(1, len(ORDER_STAGES)):
        frm, to = ORDER_STAGES[i - 1], ORDER_STAGES[i]
        out.append(ExpectedTransition(
            subject=f"order:{order.order_id}:{to}", expected_to_state=to, from_state=frm,
            expected_by=deadlines.get(to, order.required_by), failure_states=("cancelled",),
            remediation_policy=f"remediate_{to}", impact=float(order.value_cents) / 100.0))
    return tuple(out)


def evaluate_order(order: OrderExecution, *, observed_stage_states: Mapping[str, str], now: int,
                   stage_deadlines: Mapping[str, int] = None) -> Tuple[ReliabilityIncident, ...]:
    """Detect missed/overdue/diverged order transitions. ``observed_stage_states`` maps a transition subject
    (``order:<id>:<stage>``) to the observed state. Reuses the generic reliability monitor."""
    transitions = order_expected_transitions(order, stage_deadlines=stage_deadlines)
    return monitor_transitions(transitions, observed=observed_stage_states, now=now)


__all__ = ["ORDER_STAGES", "OrderLine", "OrderExecution", "order_expected_transitions", "evaluate_order"]
