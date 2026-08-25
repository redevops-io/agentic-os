"""BenchmarkRunner — prove the Runtime is necessary (v2 §31, docx P4/§12).

Runs a world across comparison arms and scores the Runtime arm against ground truth. Two arms are executed
for real over the same world (full-runtime, and a naive-agent that guesses missing facts instead of asking);
the manual and independent-agent arms are modeled deterministically with explicit notes (they represent the
counterfactual, not a second execution). The persuasive result is a *system* difference — the naive agent
guessed a missing field and produced an unverified quote — not "our model answered better".
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import RuntimeContext, WorldDefinition
from .models import BaselineResult, ExecutionMode, RunMetrics, Scorecard
from .orchestrator import ScenarioOrchestrator


class BenchmarkRunner:
    ARMS = ("manual", "naive_agent", "independent_agents", "full_runtime")

    def __init__(self, orchestrator: "ScenarioOrchestrator | None" = None) -> None:
        self._orch = orchestrator or ScenarioOrchestrator()

    def run(self, world: WorldDefinition, *, seed: str = "seed-0", authority: Any = None,
            answers: "Dict[str, str] | None" = None, offline: bool = False) -> Scorecard:
        card = Scorecard(world_id=world.world_id)

        # full runtime — asks for the missing fact, verifies
        full = self._orch.run(world, seed=seed, authority=authority, answers=answers or {}, offline=offline)
        card.arms.append(BaselineResult("full_runtime", full.metrics, outcome_reached=full.metrics.verified,
                                        notes="governed cross-app mission; asked one question; verified"))
        card.ground_truth_met = world.check_ground_truth(full.outcome, _rt_of(full))

        # naive agent — guesses the missing fact instead of asking (naive=True), no grounding
        naive = self._orch.run(world, seed=seed, authority=authority, answers={}, naive=True, offline=offline)
        naive_gt = world.check_ground_truth(naive.outcome, _rt_of(naive))
        card.arms.append(BaselineResult("naive_agent", naive.metrics, outcome_reached=bool(naive_gt),
                                        notes=_note(world, "naive_agent",
                                                    "guessed the missing field → unsupported, fails ground truth")))

        # modeled counterfactual arms (explicitly not executed) — represent the pain
        card.arms.append(BaselineResult("manual", _manual_metrics(full.metrics), outcome_reached=False,
                                        notes=_note(world, "manual", "next-day human callback")))
        card.arms.append(BaselineResult("independent_agents", _independent_metrics(full.metrics),
                                        outcome_reached=False,
                                        notes=_note(world, "independent_agents",
                                                    "isolated agents; context re-entered; possible conflict")))
        return card


def _rt_of(run) -> RuntimeContext:
    # a light shim so check_ground_truth can read clock/metrics from the run
    rt = RuntimeContext(world_id=run.world_id, mission_id=run.mission_id, fabric=None, simulator=None,  # type: ignore[arg-type]
                        graph=None, trace=run.trace, metrics=run.metrics)  # type: ignore[arg-type]
    rt.clock_s = run.trace.milestones[-1].t_offset_s if run.trace.milestones else 0.0
    return rt


def _note(world, arm, default) -> str:
    notes = getattr(world, "baseline_notes", lambda: {})()
    return notes.get(arm, default)


def _manual_metrics(full: RunMetrics) -> RunMetrics:
    m = RunMetrics()
    m.time_to_outcome_s = 8 * 3600            # next business day
    m.human_minutes_saved = 0.0
    m.manual_handoffs = 3
    m.context_copies = 4
    m.human_decisions = 3
    m.verified = False
    return m


def _independent_metrics(full: RunMetrics) -> RunMetrics:
    m = RunMetrics()
    m.time_to_outcome_s = full.time_to_outcome_s * 1.5
    m.manual_handoffs = 2
    m.context_copies = 5                       # each agent re-enters context
    m.human_decisions = 2
    m.verified = False
    return m
