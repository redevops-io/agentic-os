"""Intervention outcome learning + attribution ladder (Phase 7, plan §15).

The durable asset of a deal-closing system is not a close plan — it is a growing library of
``state → intervention → outcome`` that teaches which interventions actually move which blockers in which
contexts. Two disciplines keep that honest:

1. **Attribution ladder** — a seven-rung scale of how strongly an outcome can be attributed to an intervention:
   executed → immediate response → blocker changed → stage progressed → outcome reached → repeated/stable →
   controlled. A won deal is the strongest *signal* but the weakest *attribution* when many other actions
   happened on it; the ladder (reusing :mod:`agentic_os.attribution`'s competing-interventions discount) keeps
   "the deal closed" separate from "this action closed it".
2. **Scoped lessons** — priors are learned per (intervention kind × blocker type × segment/size/stage), shrunk
   toward neutral like the discovery/market loops, so the system learns "engaging procurement moves
   procurement blockers for mid-market" rather than "always email procurement". Priors reweight future action
   scores; they never gate a strong new signal.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from typing import ClassVar, Dict, List, Mapping, Sequence, Tuple

from ..attribution import AttributionFactors, attribution_confidence, temporal_proximity
from ..integrations.business.contracts import BusinessObject, Provenance
from .blockers import BlockerType, InterventionKind
from .candidates import CandidateAction

_DAY_S = 86_400.0


class AttributionRung(enum.IntEnum):
    """How strongly an outcome is attributable to an intervention (plan §15). Ordered."""
    NONE = 0               # not executed → no attributable effect
    EXECUTED = 1           # the action was taken
    IMMEDIATE_RESPONSE = 2  # the counterparty responded / acknowledged
    BLOCKER_CHANGED = 3    # the target condition moved toward satisfied
    STAGE_PROGRESSED = 4   # the deal advanced a methodology stage
    OUTCOME_REACHED = 5    # the deal was won (or the specific goal met)
    REPEATED_STABLE = 6    # the effect held / replicated across deals
    CONTROLLED = 7         # measured against a comparison / holdout


@dataclass(frozen=True)
class ClosingInterventionOutcome(BusinessObject):
    """One recorded state → intervention → outcome. ``competing_interventions`` is how many other actions
    happened on the deal in the attribution window — it discounts how surely THIS one gets the credit."""
    KIND: ClassVar[str] = "deal_closing.intervention_outcome"
    deal_ref: str = ""
    intervention_kind: InterventionKind = InterventionKind.NO_ACTION
    blocker_type: BlockerType = BlockerType.UNKNOWN
    target_condition: str = ""
    # context for scoped learning
    segment: str = ""
    size_band: str = ""
    stage: str = ""
    # observed signals (each maps to a rung)
    executed: bool = False
    immediate_response: bool = False
    condition_changed: bool = False
    stage_progressed: bool = False
    outcome_reached: bool = False
    repeated_stable: bool = False
    controlled: bool = False
    # measurement
    elapsed_s: float = 0.0
    competing_interventions: int = 0
    won: bool = False
    revenue_cents: int = 0


def classify_rung(o: ClosingInterventionOutcome) -> AttributionRung:
    """The highest rung the evidence supports. An unexecuted intervention is NONE — a condition that changed
    without our action having run is not our effect. Uses max over signals so a missing intermediate (e.g. no
    explicit reply) does not erase a later, observed effect."""
    if not o.executed:
        return AttributionRung.NONE
    rung = AttributionRung.EXECUTED
    if o.immediate_response:
        rung = max(rung, AttributionRung.IMMEDIATE_RESPONSE)
    if o.condition_changed:
        rung = max(rung, AttributionRung.BLOCKER_CHANGED)
    if o.stage_progressed:
        rung = max(rung, AttributionRung.STAGE_PROGRESSED)
    if o.outcome_reached or o.won:
        rung = max(rung, AttributionRung.OUTCOME_REACHED)
    if o.repeated_stable:
        rung = max(rung, AttributionRung.REPEATED_STABLE)
    if o.controlled:
        rung = max(rung, AttributionRung.CONTROLLED)
    return rung


def _competing_norm(n: int) -> float:
    return n / (n + 3.0)                       # 0 others → 0.0; grows toward 1 as alternatives pile up


def attribution_strength(o: ClosingInterventionOutcome, *, half_life_days: float = 14.0) -> float:
    """How confidently the outcome attaches to this intervention, in [0,1] — reusing the attribution engine's
    factor model. Higher rung and closer timing lift it; competing interventions discount it (if five other
    things happened on the deal, a won outcome barely attributes to any one action)."""
    rung = classify_rung(o)
    if rung is AttributionRung.NONE:
        return 0.0
    factors = AttributionFactors(
        temporal_proximity=temporal_proximity(o.elapsed_s, half_life_days * _DAY_S),
        causal_link_strength=round(int(rung) / int(AttributionRung.CONTROLLED), 4),
        competing_interventions=_competing_norm(o.competing_interventions),
        explicit_correlation=1.0 if o.controlled else 0.0,
        outcome_specificity=1.0 if rung >= AttributionRung.BLOCKER_CHANGED else 0.3,
        observation_quality=0.8,
    )
    return round(attribution_confidence(factors), 4)


# ── scoped priors ─────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Rate:
    trials: int
    successes: float
    rate: float                                # shrunk toward 0.5


def _shrunk(successes: float, trials: int, strength: int) -> float:
    return round((successes + strength * 0.5) / (trials + strength), 4)


@dataclass(frozen=True)
class ClosingPriors:
    """Learned P(this intervention resolves this blocker) at three scopes, each shrunk toward neutral."""
    by_intervention: Dict[str, _Rate]
    by_blocker_intervention: Dict[str, _Rate]
    by_context: Dict[str, _Rate]
    strength: int

    @staticmethod
    def _ctx_key(intervention: str, segment: str, size_band: str, stage: str) -> str:
        return "|".join((intervention, segment, size_band, stage))

    def _rate(self, table: Dict[str, _Rate], key: str) -> float:
        r = table.get(key)
        return r.rate if r is not None else 0.5

    def p_resolves(self, *, intervention_kind: InterventionKind, blocker_type: BlockerType = BlockerType.UNKNOWN,
                   segment: str = "", size_band: str = "", stage: str = "") -> float:
        """Best available scoped estimate: the most specific scope with evidence wins, falling back to broader
        scopes, then neutral 0.5. Specificity order: context → blocker×intervention → intervention."""
        ik = intervention_kind.value
        ctx = self._ctx_key(ik, segment, size_band, stage)
        if ctx in self.by_context:
            return self.by_context[ctx].rate
        bi = f"{blocker_type.value}|{ik}"
        if bi in self.by_blocker_intervention:
            return self.by_blocker_intervention[bi].rate
        return self._rate(self.by_intervention, ik)

    def multiplier(self, **kw) -> float:
        """A ~[0.5, 1.5] reweighting factor (rate 0.5 → 1.0 neutral)."""
        return round(0.5 + self.p_resolves(**kw), 4)


def calibrate(outcomes: Sequence[ClosingInterventionOutcome], *, strength: int = 4,
              success_rung: AttributionRung = AttributionRung.BLOCKER_CHANGED) -> ClosingPriors:
    """Learn resolution priors from recorded outcomes. A *trial* is any executed intervention; a *success* is
    one that reached ``success_rung`` (default: the blocker actually changed — the intervention's proximal job,
    a denser and more honest signal than sparse wins). Unexecuted outcomes carry no signal."""
    inv: Dict[str, List[float]] = {}           # key -> [trials, successes]
    bi: Dict[str, List[float]] = {}
    ctx: Dict[str, List[float]] = {}

    def _bump(table, key, success):
        t = table.setdefault(key, [0.0, 0.0])
        t[0] += 1
        t[1] += 1.0 if success else 0.0

    for o in outcomes:
        if not o.executed:
            continue
        success = classify_rung(o) >= success_rung
        ik = o.intervention_kind.value
        _bump(inv, ik, success)
        _bump(bi, f"{o.blocker_type.value}|{ik}", success)
        _bump(ctx, ClosingPriors._ctx_key(ik, o.segment, o.size_band, o.stage), success)

    def _rates(table):
        return {k: _Rate(trials=int(t), successes=s, rate=_shrunk(s, int(t), strength))
                for k, (t, s) in table.items()}

    return ClosingPriors(by_intervention=_rates(inv), by_blocker_intervention=_rates(bi),
                         by_context=_rates(ctx), strength=strength)


def apply_priors(actions: Sequence[CandidateAction], priors: ClosingPriors, *,
                 blocker_type: BlockerType = BlockerType.UNKNOWN, segment: str = "", size_band: str = "",
                 stage: str = "") -> Tuple[CandidateAction, ...]:
    """Reweight each action's ``p_resolves`` toward the learned scoped prior (geometric-ish blend, clamped to
    [0,1]). Learning adjusts rank; it never gates — NO_ACTION and an un-learned kind pass through unchanged."""
    out: List[CandidateAction] = []
    for a in actions:
        if a.kind is InterventionKind.NO_ACTION:
            out.append(a)
            continue
        learned = priors.p_resolves(intervention_kind=a.kind, blocker_type=blocker_type, segment=segment,
                                    size_band=size_band, stage=stage)
        blended = round(min(1.0, max(0.0, 0.5 * a.p_resolves + 0.5 * learned)), 4)
        out.append(replace(a, p_resolves=blended))
    return tuple(out)


# ── the durable library ───────────────────────────────────────────────────────────────────────────
class InterventionLedger:
    """Append-only store of ClosingInterventionOutcomes — the durable state→intervention→outcome asset. In
    memory here; an enterprise overlay persists it and joins real product/CRM telemetry."""

    def __init__(self) -> None:
        self._outcomes: List[ClosingInterventionOutcome] = []

    def record(self, outcome: ClosingInterventionOutcome) -> None:
        self._outcomes.append(outcome)

    def outcomes(self) -> Tuple[ClosingInterventionOutcome, ...]:
        return tuple(self._outcomes)

    def calibrate(self, **kw) -> ClosingPriors:
        return calibrate(self._outcomes, **kw)


__all__ = [
    "AttributionRung", "ClosingInterventionOutcome", "classify_rung", "attribution_strength",
    "ClosingPriors", "calibrate", "apply_priors", "InterventionLedger",
]
