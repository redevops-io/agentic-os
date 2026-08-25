"""ScenarioOrchestrator — start / replay a world, inject perturbations, control the clock, record outcome.

The one entry point that runs a :class:`WorldDefinition` end-to-end through the governed loop and returns a
:class:`WorldRun` (visual trace + outcome + metrics). It seeds the world's entities into the apps, wires the
Capability Fabric + Outcome Simulator, applies any failure-lab perturbations, and drives each event's
mission. Deterministic given a seed, so a run replays identically.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from runtime_contracts.world import IdentityGraph, VisualTrace

from .base import RuntimeContext, WorldDefinition
from .fabric import CapabilityDenied, CapabilityFabric
from .models import ExecutionMode, Perturbation, PerturbationKind, RunMetrics, WorldRun
from .seeder import ProjectionSeeder
from .simulator import OutcomeSimulator


class ScenarioOrchestrator:
    def __init__(self, *, seeder: Optional[ProjectionSeeder] = None) -> None:
        self._seeder = seeder or ProjectionSeeder()

    def run(self, world: WorldDefinition, *, mode: ExecutionMode = ExecutionMode.SIMULATE,
            seed: str = "seed-0", authority: Any = None, answers: Optional[Dict[str, str]] = None,
            perturbations: Optional[List[Perturbation]] = None, naive: bool = False,
            offline: bool = False) -> WorldRun:
        perturbations = perturbations or []
        graph = IdentityGraph()
        simulator = OutcomeSimulator()
        fabric = CapabilityFabric(authority=authority, simulator=simulator, mode=mode)
        world.register_capabilities(fabric)

        mission_id = f"M-{world.world_id}-{seed}"
        trace = VisualTrace(mission_id=mission_id, world_id=world.world_id)
        rt = RuntimeContext(world_id=world.world_id, mission_id=mission_id, fabric=fabric,
                            simulator=simulator, graph=graph, trace=trace, mode=mode, authority=authority,
                            answers=answers or {}, naive=naive, offline=offline)
        run = WorldRun(world_id=world.world_id, mission_id=mission_id, mode=mode.value, trace=trace,
                       metrics=rt.metrics, perturbations=[f"{p.kind.value}:{p.target}" for p in perturbations])

        events = world.event_stream(seed)
        self._apply_pre(perturbations, rt, events)

        outcome: Dict[str, Any] = {}
        for ev in events:
            self._seeder.project(ev, graph)                 # entities visible across apps, one identity
            rt.set_capsule(mission_id=mission_id, entity_id=(ev.entity_ids[0].entity_id if ev.entity_ids else ""),
                           evidence_hash=ev.content_hash)
            try:
                outcome = world.run_mission(ev, rt) or outcome
            except CapabilityDenied as e:
                # a lost capability or policy block: stop safely, never corrupt state
                rt.milestone(f"safe stop — {e.reason}", kind="policy", block="runtime")
                run.ok = False
                run.safe_stop_reason = e.reason
                break

        run.outcome = outcome
        run.needs_you = rt.needs_you
        run.metrics.ai_cost_usd = fabric.total_cost or run.metrics.ai_cost_usd
        run.metrics.api_calls = run.metrics.api_calls or len(fabric.invocations)
        gt = world.check_ground_truth(outcome, rt)
        if gt is not None:
            run.metrics.verified = run.metrics.verified and gt
        return run

    def replay(self, world: WorldDefinition, run: WorldRun, **kw: Any) -> WorldRun:
        """Re-run deterministically with the same seed — identical trace/outcome for a green run."""
        seed = run.mission_id.rsplit("-", 1)[-1]
        return self.run(world, mode=ExecutionMode(run.mode), seed=seed, **kw)

    # -- failure lab (docx P5) --
    def _apply_pre(self, perturbations: List[Perturbation], rt: RuntimeContext, events: List[Any]) -> None:
        for p in perturbations:
            if p.kind is PerturbationKind.CAPABILITY_LOSS:
                rt.fabric.disable(p.target)
            elif p.kind is PerturbationKind.MISSING_EVIDENCE:
                rt.answers.pop(p.target, None)              # force a NeedsYou by removing the autoplay answer
            elif p.kind is PerturbationKind.LATENCY:
                for provs in rt.fabric._providers.values():
                    for pr in provs:
                        pr.latency_ms += 2000
            # POLICY_CHANGE / CONFLICTING_EVIDENCE / STALE / DUPLICATE are honoured inside world missions
            rt.milestone(f"perturbation injected — {p.kind.value}", kind="policy", block="runtime")
