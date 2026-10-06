"""Recommendation pilot / shadow reconstruction (Phase 5, plan §16-§17).

Before the system is allowed to act, it must earn trust by SHADOWING: run the full chain
(state → readiness → blockers → hypotheses → close plan) over real or replayed deals at RECOMMEND autonomy — no
side effects — and measure how good the recommendations are against what humans actually judged. The scorecard is
the go/no-go evidence for raising the autonomy level.

Metrics (each abstains when the corresponding human label is absent — an unlabelled deal lowers no score):
- **blocker agreement** — does the engine's primary blocker match the human-identified one?
- **false-positive rate** — of the blockers the engine flagged, how many the human says are not real.
- **acceptance** — of the actions recommended, how many a human would accept.
- **pipeline inflation caught** — deals where the CRM stage is ahead of the evidence-derived stage (the engine
  sees the deal is really behind where the pipeline claims — the core reported-vs-verified value).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Tuple

from .autonomy import AutonomyLevel
from .blockers import InterventionKind, infer_blockers
from .candidates import candidates_from_hypothesis  # noqa: F401  (kept for parity / downstream use)
from .close_plan import compile_close_plan
from .conditions import ConditionState
from .contracts import Deal
from .hypotheses import plan_hypotheses
from .methodology import ClosingMethodology, assess_methodology


@dataclass(frozen=True)
class LabeledDeal:
    """A deal with its verified condition states and optional human labels for scoring. Any label left as the
    sentinel (None / empty) makes its metric abstain for this deal."""
    deal: Deal
    methodology: ClosingMethodology
    states: Mapping[str, ConditionState]
    primary_blocker: Optional[str] = None            # the condition a human calls the main blocker
    real_blockers: Optional[Tuple[str, ...]] = None  # conditions the human agrees are genuinely blocking
    accepted_actions: Optional[Tuple[InterventionKind, ...]] = None  # action kinds a human would accept


@dataclass(frozen=True)
class DealEvaluation:
    deal_ref: str
    engine_primary_blocker: str
    flagged_blockers: Tuple[str, ...]
    recommended_kinds: Tuple[InterventionKind, ...]
    reported_stage_idx: Optional[int]
    derived_stage_idx: int
    inflation: bool                                   # CRM stage ahead of verified
    ready_to_close: bool
    blocker_agreement: Optional[bool] = None
    false_positives: Tuple[str, ...] = ()
    accepted_hits: Optional[Tuple[int, int]] = None   # (hits, total recommended)


def _stage_idx(methodology: ClosingMethodology, stage: str) -> Optional[int]:
    return methodology.stages.index(stage) if stage in methodology.stages else None


def evaluate_deal(labeled: LabeledDeal, *, autonomy: AutonomyLevel = AutonomyLevel.RECOMMEND) -> DealEvaluation:
    """Shadow-evaluate one deal: run the chain (no side effects) and compare to the human labels where present."""
    m, states = labeled.methodology, labeled.states
    readiness = assess_methodology(m, states)
    deal_ref = labeled.deal.opportunity_ref or labeled.deal.account_ref or ""
    blockers = infer_blockers(m, readiness, states, deal_ref=deal_ref)
    hyps = plan_hypotheses(blockers)
    engine_primary = hyps[0].subject if hyps else ""
    flagged = tuple(dict.fromkeys(b.subject for b in blockers if b.subject))

    plan = compile_close_plan(labeled.deal, m, states, autonomy=autonomy)
    recommended = tuple(s.action.kind for s in plan.steps if s.action.kind is not InterventionKind.NO_ACTION)

    reported_idx = _stage_idx(m, labeled.deal.reported_stage)
    derived_idx = _stage_idx(m, readiness.derived_stage)
    derived_idx = derived_idx if derived_idx is not None else -1
    inflation = reported_idx is not None and reported_idx > derived_idx

    agreement: Optional[bool] = None
    if labeled.primary_blocker:
        agreement = (engine_primary == labeled.primary_blocker)

    false_positives: Tuple[str, ...] = ()
    if labeled.real_blockers is not None:
        real = set(labeled.real_blockers)
        false_positives = tuple(b for b in flagged if b not in real)

    accepted_hits: Optional[Tuple[int, int]] = None
    if labeled.accepted_actions is not None and recommended:
        acc = set(labeled.accepted_actions)
        accepted_hits = (sum(1 for k in recommended if k in acc), len(recommended))

    return DealEvaluation(
        deal_ref=deal_ref, engine_primary_blocker=engine_primary, flagged_blockers=flagged,
        recommended_kinds=recommended, reported_stage_idx=reported_idx, derived_stage_idx=derived_idx,
        inflation=inflation, ready_to_close=readiness.ready_to_close, blocker_agreement=agreement,
        false_positives=false_positives, accepted_hits=accepted_hits)


@dataclass(frozen=True)
class _Rate:
    num: float
    den: int
    @property
    def rate(self) -> Optional[float]:
        return round(self.num / self.den, 4) if self.den else None


@dataclass(frozen=True)
class PilotScorecard:
    n: int
    blocker_agreement: _Rate
    false_positive: _Rate                 # fraction of flagged blockers that were not real
    acceptance: _Rate                     # fraction of recommended actions a human would accept
    inflation_caught: _Rate               # fraction of deals where CRM stage > verified stage
    mean_recommendations: float
    ready_to_close: int
    evaluations: Tuple[DealEvaluation, ...]


def run_pilot(deals: Tuple[LabeledDeal, ...], *, autonomy: AutonomyLevel = AutonomyLevel.RECOMMEND) -> PilotScorecard:
    """Shadow-run the engine over a labelled deal set and aggregate the go/no-go scorecard. Nothing executes —
    the pilot is a measurement, run at RECOMMEND (or below) by construction."""
    evals = [evaluate_deal(d, autonomy=autonomy) for d in deals]

    agree_den = sum(1 for e in evals if e.blocker_agreement is not None)
    agree_num = sum(1 for e in evals if e.blocker_agreement is True)

    # false positives count only over deals that supplied a real-blocker label
    fp_num = fp_den = 0
    for e, d in zip(evals, deals):
        if d.real_blockers is not None:
            fp_den += len(e.flagged_blockers)
            fp_num += len(e.false_positives)

    acc_num = sum(e.accepted_hits[0] for e in evals if e.accepted_hits is not None)
    acc_den = sum(e.accepted_hits[1] for e in evals if e.accepted_hits is not None)

    infl_den = sum(1 for e in evals if e.reported_stage_idx is not None)
    infl_num = sum(1 for e in evals if e.inflation)

    mean_recs = round(sum(len(e.recommended_kinds) for e in evals) / len(evals), 4) if evals else 0.0
    return PilotScorecard(
        n=len(evals), blocker_agreement=_Rate(agree_num, agree_den), false_positive=_Rate(fp_num, fp_den),
        acceptance=_Rate(acc_num, acc_den), inflation_caught=_Rate(infl_num, infl_den),
        mean_recommendations=mean_recs, ready_to_close=sum(1 for e in evals if e.ready_to_close),
        evaluations=tuple(evals))


def _pct(r: _Rate) -> str:
    return f"{r.rate * 100:.0f}% ({int(r.num)}/{r.den})" if r.rate is not None else "n/a (no labels)"


def render_scorecard(card: PilotScorecard) -> str:
    """A compact markdown go/no-go scorecard — leads with the trust metrics that gate raising autonomy."""
    lines = [
        f"# Deal Closing — shadow pilot scorecard ({card.n} deals)",
        "",
        "| Metric | Result |",
        "| --- | --- |",
        f"| Blocker agreement (vs human) | {_pct(card.blocker_agreement)} |",
        f"| False-positive blockers | {_pct(card.false_positive)} |",
        f"| Recommendation acceptance | {_pct(card.acceptance)} |",
        f"| Pipeline inflation caught | {_pct(card.inflation_caught)} |",
        f"| Mean recommendations / deal | {card.mean_recommendations} |",
        f"| Deals verified ready-to-close | {card.ready_to_close}/{card.n} |",
        "",
        "_Shadow run at RECOMMEND autonomy — no side effects. Metrics abstain where human labels are absent._",
    ]
    return "\n".join(lines)


__all__ = [
    "LabeledDeal", "DealEvaluation", "PilotScorecard", "evaluate_deal", "run_pilot", "render_scorecard",
]
