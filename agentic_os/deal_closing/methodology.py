"""Closing methodology as frozen config (Phase 2, plan §9).

A methodology (MEDDIC / MEDDPICC / SPICED / a procurement checklist / a founder-led motion / a recruiting close)
is DATA, not code: it selects a subset of the normalized conditions (:mod:`agentic_os.deal_closing.conditions`),
declares dependencies between them, groups them into ordered stages, and sets evidence requirements. The engine
reasons over any methodology the same way — methodology is a PLUG-IN, not the platform.

Like a frozen experiment (:func:`agentic_os.market.experiment_design.freeze_experiment`), a compiled methodology
is content-SEALED: the seal hashes exactly the definition the readiness assessment is allowed to read, so a
methodology can't be quietly re-shaped after deals have been scored against it. ``assess_methodology`` then maps a
deal's six-valued condition states onto the methodology to answer: what stage is this deal *actually* at (by
verified evidence, not what the CRM reports), what required conditions block it, and is anything inconsistent
(a later condition satisfied while the thing it depends on is not).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Mapping, Sequence, Tuple

from runtime_contracts.protocol import content_hash

from .conditions import ConditionState, DealCondition, NORMALIZED_CONDITIONS

_VALID = frozenset(NORMALIZED_CONDITIONS)


@dataclass(frozen=True)
class ConditionSpec:
    """How one normalized condition participates in a methodology."""
    name: str                                   # one of NORMALIZED_CONDITIONS
    stage: str = ""                             # the methodology stage this condition belongs to
    required: bool = True                       # required to close vs. informative-only
    depends_on: Tuple[str, ...] = ()            # conditions that should be satisfied before this one
    min_evidence: int = 1                       # how many evidence refs the condition needs to count satisfied
    weight: float = 1.0


@dataclass(frozen=True)
class ClosingMethodology:
    """A compiled, content-sealed closing methodology. Build via :func:`compile_methodology` so it is validated
    and sealed; constructing one directly skips both."""
    KIND: ClassVar[str] = "deal_closing.methodology"
    name: str = ""
    version: str = "1"
    stages: Tuple[str, ...] = ()                 # ordered stage names, earliest → latest
    conditions: Tuple[ConditionSpec, ...] = ()
    seal: str = ""                               # content hash over the definition (anti-reshape)

    def spec(self, name: str) -> ConditionSpec | None:
        for c in self.conditions:
            if c.name == name:
                return c
        return None

    def condition_names(self) -> frozenset:
        return frozenset(c.name for c in self.conditions)

    def required_names(self) -> Tuple[str, ...]:
        return tuple(c.name for c in self.conditions if c.required)

    def for_stage(self, stage: str) -> Tuple[ConditionSpec, ...]:
        return tuple(c for c in self.conditions if c.stage == stage)


def _definition(name: str, version: str, stages: Tuple[str, ...],
                conditions: Tuple[ConditionSpec, ...]) -> dict:
    """The exact, order-independent content the seal + validation read."""
    return {
        "name": name, "version": version, "stages": list(stages),
        "conditions": sorted(
            [{"name": c.name, "stage": c.stage, "required": c.required,
              "depends_on": sorted(c.depends_on), "min_evidence": c.min_evidence, "weight": c.weight}
             for c in conditions],
            key=lambda d: d["name"]),
    }


def validate_methodology(name: str, version: str, stages: Tuple[str, ...],
                         conditions: Tuple[ConditionSpec, ...]) -> Tuple[str, ...]:
    """Return a tuple of human-readable errors (empty = valid). Checks: known condition names, unique conditions,
    stages referenced exist, dependencies are inside the methodology, and no dependency cycle."""
    errors: list[str] = []
    seen: set[str] = set()
    names = {c.name for c in conditions}
    stage_set = set(stages)
    for c in conditions:
        if c.name not in _VALID:
            errors.append(f"unknown condition {c.name!r}")
        if c.name in seen:
            errors.append(f"duplicate condition {c.name!r}")
        seen.add(c.name)
        if c.stage and c.stage not in stage_set:
            errors.append(f"condition {c.name!r} references stage {c.stage!r} not in stages")
        for dep in c.depends_on:
            if dep not in names:
                errors.append(f"condition {c.name!r} depends on {dep!r} which is not in the methodology")
    # cycle detection over depends_on
    graph = {c.name: [d for d in c.depends_on if d in names] for c in conditions}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}

    def _visit(n: str) -> bool:
        color[n] = GRAY
        for m in graph.get(n, ()):
            if color[m] == GRAY or (color[m] == WHITE and _visit(m)):
                return True
        color[n] = BLACK
        return False

    if any(color[n] == WHITE and _visit(n) for n in graph):
        errors.append("dependency cycle among conditions")
    return tuple(errors)


def compile_methodology(name: str, *, version: str = "1", stages: Sequence[str],
                        conditions: Sequence[ConditionSpec]) -> ClosingMethodology:
    """Validate and content-seal a methodology. Raises :class:`ValueError` on any validation error so an
    ill-formed methodology can never be scored against (a typo in a condition name, a dependency outside the
    set, or a cycle are all rejected up front)."""
    stages_t, conds_t = tuple(stages), tuple(conditions)
    errs = validate_methodology(name, version, stages_t, conds_t)
    if errs:
        raise ValueError(f"invalid methodology {name!r}: " + "; ".join(errs))
    seal = content_hash(_definition(name, version, stages_t, conds_t))
    return ClosingMethodology(name=name, version=version, stages=stages_t, conditions=conds_t, seal=seal)


# ── readiness assessment ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MethodologyReadiness:
    """What a methodology says about a deal, derived from VERIFIED condition states (not the CRM's stage)."""
    methodology: str
    derived_stage: str                           # furthest stage whose required conditions are all satisfied
    blocking_conditions: Tuple[str, ...]         # required & unsatisfied in the earliest incomplete stage
    unmet_required: Tuple[str, ...]              # every required condition not currently SATISFIED
    conflicts: Tuple[str, ...]                   # required conditions in CONFLICTED state
    stale: Tuple[str, ...]                       # required conditions whose evidence went STALE
    unknown: Tuple[str, ...]                     # required conditions never assessed (distinct from unsatisfied)
    inconsistencies: Tuple[str, ...]             # satisfied condition whose dependency is NOT satisfied
    ready_to_close: bool


def states_from_conditions(conditions: Sequence[DealCondition]) -> dict:
    """Collapse a deal's DealConditions into a {name: ConditionState} map (last one wins on duplicate)."""
    return {c.name: c.state for c in conditions}


def assess_methodology(methodology: ClosingMethodology,
                       states: Mapping[str, ConditionState]) -> MethodologyReadiness:
    """Map verified condition states onto the methodology. A condition absent from ``states`` is UNKNOWN. The
    derived stage is the furthest stage whose required conditions are ALL satisfied; the blocking conditions are
    the required-unsatisfied ones of the first incomplete stage. Inconsistencies flag a satisfied condition whose
    dependency is not satisfied (classic reported-out-of-order)."""
    def state_of(name: str) -> ConditionState:
        return states.get(name, ConditionState.UNKNOWN)

    required = methodology.required_names()
    unmet = tuple(n for n in required if state_of(n) is not ConditionState.SATISFIED)
    conflicts = tuple(n for n in required if state_of(n) is ConditionState.CONFLICTED)
    stale = tuple(n for n in required if state_of(n) is ConditionState.STALE)
    unknown = tuple(n for n in required if state_of(n) is ConditionState.UNKNOWN)

    # derived stage: walk stages in order; a stage is complete iff all its required conditions are SATISFIED
    derived = ""
    first_incomplete = ""
    for stage in methodology.stages:
        reqs = [c.name for c in methodology.for_stage(stage) if c.required]
        complete = all(state_of(n) is ConditionState.SATISFIED for n in reqs)
        if complete:
            derived = stage
        else:
            first_incomplete = stage
            break

    if first_incomplete:
        blocking = tuple(c.name for c in methodology.for_stage(first_incomplete)
                         if c.required and state_of(c.name) is not ConditionState.SATISFIED)
    else:
        blocking = ()

    inconsistencies = tuple(
        c.name for c in methodology.conditions
        if state_of(c.name) is ConditionState.SATISFIED
        and any(state_of(dep) is not ConditionState.SATISFIED for dep in c.depends_on)
    )

    ready = not unmet and not conflicts
    return MethodologyReadiness(
        methodology=methodology.name, derived_stage=derived, blocking_conditions=blocking,
        unmet_required=unmet, conflicts=conflicts, stale=stale, unknown=unknown,
        inconsistencies=inconsistencies, ready_to_close=ready)


__all__ = [
    "ConditionSpec", "ClosingMethodology", "MethodologyReadiness",
    "compile_methodology", "validate_methodology", "assess_methodology", "states_from_conditions",
]
