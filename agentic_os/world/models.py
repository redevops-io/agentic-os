"""Shared value types for the world-runtime: execution modes, perturbations, run result, metrics, scorecard.

The world-runtime orchestrates a dataset world (``runtime_contracts.world``) end-to-end through the same
governed loop the product uses — Discovery → plan → governed capability execution → NeedsYou → verified
outcome → reconciliation — and emits a ``VisualTrace`` for the animated canvas plus business/agentic/runtime
metrics and (where the world has ground truth) a scorecard against baselines.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ExecutionMode(str, Enum):
    """How consequential capability execution is (v2 §17). The public demo must label anything that does
    not write to a real external system as SIMULATE/SHADOW rather than presenting it as live."""
    DRY_RUN = "DRY_RUN"      # compile + validate only
    SIMULATE = "SIMULATE"    # execute against fixtures / the outcome simulator
    SHADOW = "SHADOW"        # real evidence, side effects suppressed
    LIVE = "LIVE"            # execute approved actions for real


class PerturbationKind(str, Enum):
    """The failure-lab injections (docx P5) — the orchestrator applies these and the mission must recover,
    replan, compensate, ask for help, or safely stop — never silently corrupt state."""
    MISSING_EVIDENCE = "missing_evidence"
    LATENCY = "latency"
    PROVIDER_FAILURE = "provider_failure"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    POLICY_CHANGE = "policy_change"
    CAPABILITY_LOSS = "capability_loss"
    STALE_EVIDENCE = "stale_evidence"
    DUPLICATE_EVENT = "duplicate_event"


@dataclass(frozen=True)
class Perturbation:
    kind: PerturbationKind
    target: str = ""          # a capability id / evidence key / policy the perturbation hits
    detail: str = ""


@dataclass
class RunMetrics:
    """Buyer-facing outcome metrics + the agentic/runtime metrics that expose the cost of a poorly
    functioning AI system (v2 §20). Every figure is produced by the run, not asserted."""
    # business
    time_to_first_response_s: float = 0.0
    time_to_outcome_s: float = 0.0
    revenue_value: float = 0.0
    human_minutes_saved: float = 0.0
    # agentic quality
    manual_handoffs: int = 0
    context_copies: int = 0
    unsupported_guesses: int = 0
    human_decisions: int = 0
    evidence_completeness: float = 1.0
    verified: bool = False
    # runtime
    api_calls: int = 0
    ai_cost_usd: float = 0.0
    replans: int = 0
    policy_blocks: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class WorldRun:
    """The result of orchestrating one world through the runtime: the visual trace, the business outcome,
    the metrics, and the mode/realism it ran under."""
    world_id: str
    mission_id: str
    mode: str
    trace: Any                       # runtime_contracts.world.VisualTrace
    outcome: Dict[str, Any] = field(default_factory=dict)
    metrics: RunMetrics = field(default_factory=RunMetrics)
    needs_you: List[Dict[str, str]] = field(default_factory=list)
    perturbations: List[str] = field(default_factory=list)
    ok: bool = True
    safe_stop_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"world_id": self.world_id, "mission_id": self.mission_id, "mode": self.mode,
                "outcome": self.outcome, "metrics": self.metrics.to_dict(), "needs_you": self.needs_you,
                "perturbations": self.perturbations, "ok": self.ok,
                "safe_stop_reason": self.safe_stop_reason,
                "trace": self.trace.canonical_form() if self.trace is not None else None}


@dataclass
class BaselineResult:
    """One arm of the cross-app benchmark (v2 §31 / docx P4) — manual, naive agent, independent agents,
    or the full Runtime — with the metrics that separate 'AI answered' from 'outcome completed'."""
    arm: str
    metrics: RunMetrics
    outcome_reached: bool = False
    notes: str = ""


@dataclass
class Scorecard:
    """A world's benchmark result: per-arm metrics + the ground-truth score for the Runtime arm."""
    world_id: str
    arms: List[BaselineResult] = field(default_factory=list)
    ground_truth_met: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"world_id": self.world_id, "ground_truth_met": self.ground_truth_met,
                "arms": [{"arm": a.arm, "outcome_reached": a.outcome_reached, "notes": a.notes,
                          "metrics": a.metrics.to_dict()} for a in self.arms]}
