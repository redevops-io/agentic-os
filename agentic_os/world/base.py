"""WorldDefinition + RuntimeContext — the seam every dataset world implements.

A world declares its descriptor, its event stream (from a seed), the capabilities it needs, the governed
mission that runs over an event, and (optionally) its ground-truth check. The orchestrator supplies a
:class:`RuntimeContext` carrying the fabric, simulator, identity graph, the growing visual trace, the
metrics, a virtual clock, and pre-supplied answers (so a WATCH autoplay resolves NeedsYou deterministically
while EXPLORE/interactive leaves them open). Adding a world is a definition, not new engine code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from runtime_contracts.world import (
    Capsule,
    IdentityGraph,
    TraceMilestone,
    VisualTrace,
    WorldDescriptor,
    WorldEvent,
)

from .fabric import CapabilityFabric
from .models import ExecutionMode, RunMetrics
from .simulator import OutcomeSimulator


@dataclass
class RuntimeContext:
    world_id: str
    mission_id: str
    fabric: CapabilityFabric
    simulator: OutcomeSimulator
    graph: IdentityGraph
    trace: VisualTrace
    metrics: RunMetrics = field(default_factory=RunMetrics)
    mode: ExecutionMode = ExecutionMode.SIMULATE
    authority: Any = None
    answers: Dict[str, str] = field(default_factory=dict)     # pre-supplied NeedsYou answers (autoplay)
    needs_you: List[Dict[str, str]] = field(default_factory=list)
    clock_s: float = 0.0
    naive: bool = False                                       # baseline arm: guess instead of asking
    offline: bool = False                                     # force fixtures instead of live network fetch
    _capsule: Optional[Capsule] = None

    def tick(self, dt: float) -> float:
        self.clock_s = round(self.clock_s + dt, 3)
        return self.clock_s

    def set_capsule(self, **kw: str) -> None:
        base = self._capsule.__dict__ if self._capsule else {}
        self._capsule = Capsule(**{**base, **{k: v for k, v in kw.items() if v}})

    def milestone(self, label: str, *, kind: str = "event", node: str = "", block: str = "runtime",
                  needs_you: str = "", realism: str = "") -> None:
        self.trace.add(TraceMilestone(self.clock_s, label, kind=kind, node=node, block=block,
                                      capsule=self._capsule, needs_you=needs_you, realism=realism))

    def ask(self, reason: str, question: str) -> Optional[str]:
        """Raise a NeedsYou. The full runtime returns the pre-supplied answer (autoplay) or None (parks). A
        naive-agent baseline instead *guesses* — it fills the field to keep going, counted as an unsupported
        guess (that is precisely the failure the governed loop avoids)."""
        if self.naive:
            self.metrics.unsupported_guesses += 1
            return self.answers.get(question) or "GUESS"      # proceed on a guess; no human involved
        self.needs_you.append({"reason": reason, "question": question})
        self.metrics.human_decisions += 1
        return self.answers.get(question) or self.answers.get(reason)


class WorldDefinition:
    """Base class for a dataset world. Subclasses override descriptor/event_stream/register_capabilities/
    run_mission and (optionally) check_ground_truth + baseline notes."""

    world_id: str = ""

    def descriptor(self) -> WorldDescriptor:
        raise NotImplementedError

    def event_stream(self, seed: str) -> List[WorldEvent]:
        raise NotImplementedError

    def register_capabilities(self, fabric: CapabilityFabric) -> None:
        raise NotImplementedError

    def run_mission(self, event: WorldEvent, rt: RuntimeContext) -> Dict[str, Any]:
        """Execute the governed loop over one event; append trace milestones + metrics; return the outcome."""
        raise NotImplementedError

    def check_ground_truth(self, outcome: Dict[str, Any], rt: RuntimeContext) -> Optional[bool]:
        return None
