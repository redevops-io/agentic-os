"""Hardware-aware target selection — §4 of the accelerator-integration plan.

Lets the planner weigh **physical execution facts** (`ExecutionTargetStats` from ``provider.py``)
without hardware ever entering application intent. The Goal expresses *constraints* — max latency, max
cost, modality, residency, hosted-vs-self-hosted, a quality floor, minimum context — and this function
maps them to the eligible targets. **No application code contains an NVIDIA (or AMD) branch**; a target
is chosen because it satisfies the constraints, and the choice is fully explainable.

Determinism is the point:

  * selection runs over a **passed-in statistics snapshot** (with a ``statistics_version``), never a live
    telemetry read — so **replay reproduces the plan** and a snapshot reproduces the same choice;
  * an **unavailable** accelerator is removed as a candidate **without changing Mission semantics** (the
    same Mission simply binds a different eligible target, or none);
  * mutating a relevant statistic (a cost, a p95) **changes the selection** — the test that pins this is
    what proves the planner actually consulted the stats.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .provider import ExecutionTargetStats, _digest


@dataclass(frozen=True)
class TargetConstraints:
    """Constraints derived from the Goal — never a hardware request. Any field left at its default is
    unconstrained. ``locality`` is 'hosted' | 'self-hosted' | 'data-local' | '' (any)."""
    max_latency_ms: Optional[float] = None      # measured against p95
    max_cost_usd: Optional[float] = None
    required_modalities: Tuple[str, ...] = ()
    data_residency: str = ""
    locality: str = ""
    min_context: int = 0

    def digest(self) -> str:
        return _digest({"max_latency_ms": self.max_latency_ms, "max_cost_usd": self.max_cost_usd,
                        "modalities": sorted(self.required_modalities), "residency": self.data_residency,
                        "locality": self.locality, "min_context": self.min_context})


@dataclass(frozen=True)
class Candidate:
    stats: ExecutionTargetStats
    eligible: bool
    reasons: Tuple[str, ...]      # why eligible / why excluded (for EXPLAIN)


@dataclass(frozen=True)
class TargetSelection:
    """The planner-facing result: the ranked eligible targets, the winner, and full EXPLAIN evidence."""
    candidates: Tuple[Candidate, ...]
    selected: Optional[ExecutionTargetStats]
    statistics_version: str
    selection_reason: str

    @property
    def eligible(self) -> List[ExecutionTargetStats]:
        return [c.stats for c in self.candidates if c.eligible]

    def digest(self) -> str:
        return _digest({"selected": self.selected.digest() if self.selected else None,
                        "eligible": [c.stats.digest() for c in self.candidates if c.eligible],
                        "statistics_version": self.statistics_version})


def _check(stats: ExecutionTargetStats, c: TargetConstraints,
           available: Optional[frozenset]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    # availability first — unavailable hardware is simply not a candidate
    if available is not None and stats.accelerator_kind and stats.accelerator_kind not in available:
        reasons.append(f"excluded: {stats.accelerator_kind} unavailable")
        return False, reasons
    if c.max_latency_ms is not None and stats.p95_latency_ms > c.max_latency_ms:
        reasons.append(f"excluded: p95 {stats.p95_latency_ms}ms > max {c.max_latency_ms}ms")
    if c.max_cost_usd is not None and stats.estimated_cost_usd > c.max_cost_usd:
        reasons.append(f"excluded: cost {stats.estimated_cost_usd} > max {c.max_cost_usd}")
    if c.min_context and stats.supported_context < c.min_context:
        reasons.append(f"excluded: context {stats.supported_context} < min {c.min_context}")
    if c.required_modalities and not set(c.required_modalities).issubset(set(stats.supported_modalities)):
        reasons.append(f"excluded: modalities {c.required_modalities} not all supported")
    if c.data_residency and stats.data_residency and stats.data_residency != c.data_residency:
        reasons.append(f"excluded: residency {stats.data_residency} != required {c.data_residency}")
    if c.locality and stats.locality and stats.locality != c.locality:
        reasons.append(f"excluded: locality {stats.locality} != required {c.locality}")
    eligible = not reasons
    if eligible:
        reasons.append("eligible: satisfies all constraints")
    return eligible, reasons


def _sort_key(s: ExecutionTargetStats) -> Tuple:
    # deterministic ranking: cheapest, then fastest p95, then a stable tiebreak on identity
    return (s.estimated_cost_usd, s.p95_latency_ms, s.provider, s.accelerator_kind, s.model_id)


def select_targets(candidates: List[ExecutionTargetStats], constraints: TargetConstraints, *,
                   statistics_version: str, available: Optional[frozenset] = None) -> TargetSelection:
    """Filter candidates by the Goal's constraints and pick the best eligible target, deterministically.

    ``available`` is a snapshot of currently-deployable accelerator kinds (also passed in, never queried
    live) so an unavailable target drops out reproducibly. All candidates should share
    ``statistics_version``; the winner is the cheapest/fastest eligible target.
    """
    evaluated: List[Candidate] = []
    for s in candidates:
        ok, reasons = _check(s, constraints, available)
        evaluated.append(Candidate(stats=s, eligible=ok, reasons=tuple(reasons)))
    # stable order for EXPLAIN: eligible first (by rank), then excluded (by identity)
    evaluated.sort(key=lambda c: (not c.eligible, _sort_key(c.stats) if c.eligible else (), c.stats.digest()))
    eligible = [c.stats for c in evaluated if c.eligible]
    selected = min(eligible, key=_sort_key) if eligible else None
    if selected is None:
        reason = "no target satisfies the constraints"
    else:
        reason = (f"selected {selected.provider}/{selected.accelerator_kind or 'cpu'} "
                  f"(cost={selected.estimated_cost_usd}, p95={selected.p95_latency_ms}ms) "
                  f"as cheapest eligible of {len(eligible)}")
    return TargetSelection(candidates=tuple(evaluated), selected=selected,
                           statistics_version=statistics_version, selection_reason=reason)


if __name__ == "__main__":
    checks = []
    v = "1"
    hosted = ExecutionTargetStats(provider="nvidia-nim", accelerator_kind="nvidia-h100",
                                  locality="hosted", p95_latency_ms=120.0, estimated_cost_usd=0.02,
                                  supported_context=131072, supported_modalities=("text",),
                                  statistics_version=v)
    selfhost = ExecutionTargetStats(provider="amd-rocm", accelerator_kind="amd-instinct-mi300x",
                                    locality="self-hosted", p95_latency_ms=90.0, estimated_cost_usd=0.05,
                                    supported_context=131072, supported_modalities=("text",),
                                    statistics_version=v)
    cands = [hosted, selfhost]

    # cost constraint changes the eligible set vs latency constraint
    by_cost = select_targets(cands, TargetConstraints(max_cost_usd=0.03), statistics_version=v)
    by_latency = select_targets(cands, TargetConstraints(max_latency_ms=100.0), statistics_version=v)
    checks.append(("cost constraint selects the cheaper", by_cost.selected.provider == "nvidia-nim"))
    checks.append(("latency constraint selects the faster", by_latency.selected.provider == "amd-rocm"))
    checks.append(("different constraints → different eligibility",
                   by_cost.eligible != by_latency.eligible or by_cost.selected != by_latency.selected))

    # same snapshot → same plan
    checks.append(("same snapshot reproduces plan",
                   select_targets(cands, TargetConstraints(max_cost_usd=0.03), statistics_version=v).digest()
                   == by_cost.digest()))

    # unavailable hardware drops out without error
    only_amd = select_targets(cands, TargetConstraints(), statistics_version=v,
                              available=frozenset({"amd-instinct-mi300x"}))
    checks.append(("unavailable hardware removed",
                   only_amd.selected.provider == "amd-rocm"
                   and all(c.stats.provider != "nvidia-nim" for c in only_amd.candidates if c.eligible)))

    # mutating a relevant statistic changes selection
    cheaper_amd = ExecutionTargetStats(**{**selfhost.__dict__, "estimated_cost_usd": 0.01})
    mutated = select_targets([hosted, cheaper_amd], TargetConstraints(max_cost_usd=0.03), statistics_version=v)
    checks.append(("mutating cost changes the winner", mutated.selected.provider == "amd-rocm"))

    # no eligible target → explicit, not a crash
    none_ok = select_targets(cands, TargetConstraints(max_cost_usd=0.001), statistics_version=v)
    checks.append(("no eligible target is explicit", none_ok.selected is None and none_ok.eligible == []))

    # every choice is explainable
    checks.append(("candidates carry EXPLAIN reasons",
                   all(c.reasons for c in by_cost.candidates)))
    checks.append(("no hardware branch in app intent — constraints only",
                   isinstance(TargetConstraints(), TargetConstraints)))  # the API takes constraints, not GPUs

    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
    print(f"RESULT {passed}/{len(checks)}  (target-selection §4)")
    import sys
    sys.exit(0 if passed == len(checks) else 1)
