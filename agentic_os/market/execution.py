"""Governed experiment execution (Market-Intelligence plan §12, Phase 6).

The action side — and it stops at the gate. A proposed :class:`Experiment` is staged on an approval queue;
only an approved experiment is handed to an :class:`ExperimentExecutor`, and only a ``no_change`` experiment is
never runnable. The default :class:`DryRunExecutor` renders the proposed change as a reviewable artifact and
makes NO site change; a real executor (page/content generation → a site PR, analytics instrumentation,
controlled rollout via the Mission Runtime) plugs in behind the same seam. Consistent with the rest of the
stack: recommend and prepare, human approves, execution is separate and governed — nothing autonomous.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Tuple

from .contracts import Experiment


@dataclass(frozen=True)
class ExecutionResult:
    experiment_ref: str
    status: str                        # dry_run | executed | refused
    detail: str = ""
    artifact: str = ""                 # the rendered change (dry-run) or a receipt/PR ref (executed)


class ExperimentExecutor(Protocol):
    def execute(self, experiment: Experiment) -> ExecutionResult: ...


@dataclass
class DryRunExecutor:
    """Renders the proposed change; changes nothing. The safe default until a real Mission executor is wired."""
    def execute(self, experiment: Experiment) -> ExecutionResult:
        art = f"[DRY-RUN] {experiment.site}: {experiment.change}\nwhy: {experiment.explain}"
        return ExecutionResult(experiment.prov.provider_ref, "dry_run",
                               "rendered proposed change; no site change made", artifact=art)


@dataclass
class ExperimentQueue:
    """Approval-gated queue for planned experiments. Approve → execute via `executor` (DryRunExecutor unless a
    real one is injected AND `execute_enabled`); dismiss → parked. A `no_change` experiment cannot be run."""
    executor: ExperimentExecutor = field(default_factory=DryRunExecutor)
    execute_enabled: bool = False
    _decisions: Dict[str, str] = field(default_factory=dict)      # ref -> approved | dismissed
    _results: Dict[str, ExecutionResult] = field(default_factory=dict)

    def approve(self, experiment: Experiment) -> ExecutionResult:
        ref = experiment.prov.provider_ref
        if experiment.decision != "proposed":
            return ExecutionResult(ref, "refused", "experiment is no_change — not runnable")
        self._decisions[ref] = "approved"
        # a DryRunExecutor is always safe; a real executor only runs when explicitly enabled
        dry = self.execute_enabled is False and not isinstance(self.executor, DryRunExecutor)
        res = DryRunExecutor().execute(experiment) if dry else self.executor.execute(experiment)
        self._results[ref] = res
        return res

    def dismiss(self, experiment: Experiment) -> None:
        self._decisions[experiment.prov.provider_ref] = "dismissed"

    def status(self, experiment_ref: str) -> str:
        return self._decisions.get(experiment_ref, "staged")

    def result(self, experiment_ref: str) -> Optional[ExecutionResult]:
        return self._results.get(experiment_ref)
