"""Learning — FOUR loops, not one. Plus Reflection before Learning.

"Learning" conflates four different problems with different signals; they must not share a
model:

  * Retrieval Learning  — which context to pull per capability      (exists in Context Runtime)
  * Planning Learning    — intent shape + workflow ordering
  * Capability Learning  — which operator performs a capability best (updates manifest confidence)
  * Business Learning     — did the COMPANY achieve the objective?    (mission KPI)

A MissionOutcomeEvent is routed to the right loop. Reflection runs FIRST: a pass over the
mission trace emits a structured Lesson (failed assumption / missing verification / better
capability) — far higher-signal training data for Planning Learning than a scalar reward.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable

from .registry import CapabilityRegistry
from .types import MissionOutcomeEvent, Lesson


class LearningRouter:
    """Routes each mission outcome to the four loops. In prod each loop is a separate learner
    fed off the Kafka bus; here they're in-memory accumulators with pluggable callbacks."""

    def __init__(self, registry: CapabilityRegistry | None = None):
        self.registry = registry
        # Capability Learning: rolling success rate per capability (updates manifest confidence)
        self._cap_stats: dict[str, list[int]] = defaultdict(list)
        # Planning Learning: success rate per plan signature (workflow ordering)
        self._plan_stats: dict[str, list[int]] = defaultdict(list)
        # Business Learning: realized value per mission kind
        self._business: dict[str, float] = defaultdict(float)
        # Verification-policy Learning: acceptance rate per capability (which checks earn their cost)
        self._verify_stats: dict[str, list[int]] = defaultdict(list)
        self.retrieval_hook: Callable[[MissionOutcomeEvent], None] | None = None

    def record(self, ev: MissionOutcomeEvent) -> None:
        s = 1 if ev.success else 0
        self._business[ev.kind] += ev.business_value
        if ev.plan_signature:
            self._plan_stats[ev.plan_signature].append(s)
        if self.retrieval_hook:
            self.retrieval_hook(ev)

    # ── bus wiring: fold the mission event stream off the serving path ──────────
    def attach(self, store) -> None:
        """Consume the mission event bus (the aggregator pattern): fold NodeSucceeded/NodeFailed
        into Capability Learning and MissionOutcome into Planning/Business Learning. Use this for a
        learner replica; the in-process runtime also folds inline for immediate same-run feedback."""
        store.subscribe(self._on_event)

    def _on_event(self, ev) -> None:
        t = ev.type
        if t == "NodeSucceeded":
            self.record_capability(ev.payload.get("capability", ""), True)
        elif t == "NodeFailed":
            self.record_capability(ev.payload.get("capability", ""), False)
        elif t == "MissionOutcome":
            p = ev.payload
            self._business[p.get("kind", "mission")] += float(p.get("business_value", 0.0))
            sig = p.get("plan_signature")
            if sig:
                self._plan_stats[sig].append(1 if p.get("success") else 0)

    def policy(self) -> dict:
        """What's been learned, per loop — for EXPLAIN / the cockpit."""
        avg = lambda v: round(sum(v) / len(v), 3)
        return {
            "capability": {k: avg(v) for k, v in self._cap_stats.items() if v},   # Capability Learning
            "planning": {k: avg(v) for k, v in self._plan_stats.items() if v},    # Planning Learning
            "business": dict(self._business),                                     # Business Learning
            "verification": {k: avg(v) for k, v in self._verify_stats.items() if v},  # Verification-policy
        }

    def record_capability(self, capability: str, success: bool) -> None:
        self._cap_stats[capability].append(1 if success else 0)
        if self.registry:
            cap = self.registry.get(capability)
            if cap:
                hist = self._cap_stats[capability]
                cap.confidence = round(sum(hist) / len(hist), 3)   # learned confidence feeds the planner

    def record_verification(self, capability: str, accepted: bool) -> None:
        """Verification-policy Learning: track how often a capability's result passes acceptance."""
        self._verify_stats[capability].append(1 if accepted else 0)

    def capability_confidence(self, capability: str) -> float | None:
        hist = self._cap_stats.get(capability)
        return round(sum(hist) / len(hist), 3) if hist else None

    def plan_success(self, signature: str) -> float | None:
        hist = self._plan_stats.get(signature)
        return round(sum(hist) / len(hist), 3) if hist else None

    def business_value(self, kind: str) -> float:
        return self._business.get(kind, 0.0)


def reflect(mission_id: str, succeeded: bool, timeline: list[dict],
            registry: CapabilityRegistry | None = None, plan=None) -> Lesson:
    """Reflection BEFORE learning: scan the trace for the failure signature and emit a structured
    lesson. When a registry + plan are supplied it proposes a concrete alternative capability for the
    failed outcome — high-signal input to Planning Learning (a thinking model replaces the heuristic)."""
    if succeeded:
        return Lesson(mission_id=mission_id, success=True,
                      evidence=[f"{len(timeline)} events, no failures"])
    failed = [e for e in timeline if e["type"] == "NodeFailed"]
    compensated = [e for e in timeline if e["type"] == "NodeCompensated"]
    had_approval = any(e["type"] == "NodeParked" for e in timeline)
    cap = (failed[0]["payload"].get("capability") if failed else "") or ""
    better = ""
    if failed and registry is not None and plan is not None:
        node = plan.graph.by_id(failed[0]["payload"].get("node_id"))
        if node is not None and node.produces:
            alts = [c.name for c in registry.providing(node.produces) if c.name != cap]
            better = alts[0] if alts else ""
    return Lesson(
        mission_id=mission_id,
        success=False,
        failed_assumption=f"capability '{cap}' would succeed" if cap else "plan would complete",
        missing_verification=("" if had_approval else "no human/verification gate before the failing side effect"),
        better_capability=better,   # a concrete alternative provider for the failed outcome, if any
        evidence=[f"{len(failed)} failed node(s), {len(compensated)} compensated"],
    )
