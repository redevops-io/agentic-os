"""Outcome-driven learning (Market-Intelligence plan §13, Phase 7).

Close the loop: measured :class:`ExperimentOutcome`s become per-site, per-pattern priors that reweight future
opportunities — so a pattern that *helped us* is favored and one that *didn't* is damped, regardless of how
prevalent it is among competitors. This is the plan's core discipline made mechanical: **first-party outcomes
supersede imitation.** Deterministic, with shrinkage so a single result can't dominate.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .contracts import ExperimentOutcome, Opportunity, Provenance

_RESULT_VALUE = {"improved": 1.0, "no_effect": 0.0, "worse": -1.0}


@dataclass(frozen=True)
class PatternPrior:
    """Learned usefulness of a pattern on a site: shrunk mean outcome in [-1, 1] over `n` verified runs, and a
    multiplier applied to expected value (>1 favor, <1 damp)."""
    key: str                           # "<site>|<pattern_ref>"
    n: int
    mean_outcome: float
    multiplier: float


def learn_priors(outcomes: Sequence[ExperimentOutcome], *, shrinkage: float = 2.0) -> Dict[str, PatternPrior]:
    """Per (site, pattern) shrunk-mean outcome → an EV multiplier. Shrinkage pulls thin samples toward neutral
    (0) so one run can't swing a pattern; multiplier = 1 + shrunk_mean (bounded to [0.1, 2.0])."""
    groups: Dict[str, List[float]] = defaultdict(list)
    for o in outcomes:
        groups[f"{o.site}|{o.pattern_ref}"].append(_RESULT_VALUE.get(o.result, 0.0))
    priors: Dict[str, PatternPrior] = {}
    for key, vals in groups.items():
        n = len(vals)
        shrunk = sum(vals) / (n + shrinkage)           # toward 0 for small n
        mult = max(0.1, min(2.0, 1.0 + shrunk))
        priors[key] = PatternPrior(key=key, n=n, mean_outcome=round(shrunk, 3), multiplier=round(mult, 3))
    return priors


def reweight_opportunities(opportunities: Sequence[Opportunity],
                           priors: Dict[str, PatternPrior]) -> Tuple[Opportunity, ...]:
    """Reweight each opportunity's expected value by its learned prior (site+pattern). Opportunities with no
    prior yet are unchanged (explore). Returns them re-ranked by the learned expected value, descending."""
    out: List[Opportunity] = []
    for o in opportunities:
        prior = priors.get(f"{o.site}|{o.pattern_ref}")
        if prior is None:
            out.append(o)
            continue
        ev = round(min(1.0, o.expected_value * prior.multiplier), 4)
        out.append(Opportunity(
            prov=Provenance(o.prov.provider, o.prov.provider_ref, o.prov.evidence_refs),
            pattern_ref=o.pattern_ref, site=o.site, gap=o.gap, proposed_experiment=o.proposed_experiment,
            expected_value=ev, reversibility=o.reversibility, evidence_refs=o.evidence_refs,
            confidence=o.confidence))
    out.sort(key=lambda o: o.expected_value, reverse=True)
    return tuple(out)
