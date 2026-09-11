"""Research Information-Gain Planner — decide what to investigate next to reduce uncertainty the most
(AGENTIC_APPS_PROACTIVE_INTELLIGENCE_PLAN §10).

Most research agents optimise 'what information answers the question?'. This planner additionally asks
'what investigation would reduce uncertainty the most, per unit of cost?' — and knows when to stop.

    belief over hypotheses → for each candidate investigation, expected information gain / cost →
    choose the best (or STOP: decision reached / gain exhausted / budget spent / irreducibly ambiguous)
    → acquire evidence → Bayesian belief update → repeat

It is deterministic and model-free: information gain is expected entropy reduction (mutual information)
computed from each investigation's outcome likelihoods; the update is Bayes' rule. Whether this
*policy* actually reaches correct conclusions with less budget than naive strategies is decided by
:mod:`agentic_os.research_planner_backtest` on controlled tasks — not asserted here. As with the other
kernels, that proves the planning LOGIC on a controlled benchmark, not real-world research skill.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


def _entropy(probs: Sequence[float]) -> float:
    """Shannon entropy in bits; 0 for a certain belief, log2(n) for a uniform one."""
    return -sum(p * math.log2(p) for p in probs if p > 1e-12)


@dataclass(frozen=True)
class Belief:
    """A probability distribution over competing hypotheses (normalised on construction)."""
    probs: Mapping[str, float]

    def __post_init__(self):
        total = sum(self.probs.values())
        if total <= 0:
            raise ValueError("belief must have positive total mass")
        object.__setattr__(self, "probs", {h: p / total for h, p in self.probs.items()})

    @property
    def entropy(self) -> float:
        return _entropy(list(self.probs.values()))

    @property
    def leading(self) -> Tuple[str, float]:
        h = max(self.probs, key=self.probs.get)
        return h, self.probs[h]

    @staticmethod
    def uniform(hypotheses: Sequence[str]) -> "Belief":
        return Belief({h: 1.0 for h in hypotheses})


@dataclass(frozen=True)
class Investigation:
    """A possible evidence-acquisition. ``pos_likelihoods[h]`` = P(this yields a 'positive' outcome |
    hypothesis h); the complement is the 'negative' outcome. A diagnostic investigation's likelihoods
    differ sharply across hypotheses (informative); a useless one is ~equal across all (≈0 gain)."""
    id: str
    cost: float
    pos_likelihoods: Mapping[str, float]
    question: str = ""


def _outcome_probs(belief: Belief, inv: Investigation) -> Tuple[float, float]:
    """Marginal P(positive), P(negative) under the current belief."""
    p_pos = sum(belief.probs[h] * inv.pos_likelihoods.get(h, 0.5) for h in belief.probs)
    return p_pos, 1.0 - p_pos


def update(belief: Belief, inv: Investigation, positive: bool) -> Belief:
    """Bayesian posterior after observing an outcome of ``inv``."""
    post = {}
    for h, prior in belief.probs.items():
        like = inv.pos_likelihoods.get(h, 0.5)
        post[h] = prior * (like if positive else (1.0 - like))
    if sum(post.values()) <= 0:                      # contradictory evidence — keep the prior
        return belief
    return Belief(post)


def expected_information_gain(belief: Belief, inv: Investigation) -> float:
    """Mutual information between the investigation's outcome and the hypothesis, in bits:
    H(belief) − E_outcome[H(posterior)]. Always ≥ 0; 0 when the investigation can't discriminate."""
    p_pos, p_neg = _outcome_probs(belief, inv)
    h_prior = belief.entropy
    h_post = 0.0
    if p_pos > 1e-12:
        h_post += p_pos * update(belief, inv, True).entropy
    if p_neg > 1e-12:
        h_post += p_neg * update(belief, inv, False).entropy
    return max(0.0, h_prior - h_post)


# ── the planning step (plan §10) ─────────────────────────────────────────────────────
class ResearchAction(Enum):
    INVESTIGATE = "investigate"
    STOP = "stop"


class StopReason(Enum):
    DECISION_REACHED = "decision_reached"           # a hypothesis passed the decision threshold
    GAIN_EXHAUSTED = "gain_exhausted"               # no remaining investigation reduces uncertainty enough
    BUDGET_EXHAUSTED = "budget_exhausted"           # not enough budget for any affordable investigation
    IRREDUCIBLY_AMBIGUOUS = "irreducibly_ambiguous" # uncertainty remains but nothing can reduce it further


@dataclass(frozen=True)
class ResearchPolicy:
    decision_threshold: float = 0.9    # leading hypothesis ≥ this ⇒ decide
    min_gain_bits: float = 0.02        # a best gain below this ⇒ not worth continuing
    cost_weight: float = 1.0           # >0 ranks by gain/cost^cost_weight; 0 ranks by raw gain (greedy)


@dataclass(frozen=True)
class ResearchStep:
    action: ResearchAction
    belief: Belief
    expected_gain: float = 0.0                 # bits the chosen investigation is expected to yield
    investigation: Optional[Investigation] = None
    stop_reason: Optional[StopReason] = None
    decision: Optional[str] = None             # the leading hypothesis (always reported)
    rationale: str = ""


def _efficiency(gain: float, cost: float, cost_weight: float) -> float:
    if cost_weight <= 0:
        return gain                             # greedy: ignore cost
    return gain / (max(cost, 1e-9) ** cost_weight)


def plan(belief: Belief, investigations: Sequence[Investigation], budget_remaining: float,
         policy: Optional[ResearchPolicy] = None) -> ResearchStep:
    """Decide the single best next move: investigate (which one) or stop (why). Chooses the affordable
    investigation with the highest expected information gain per unit cost; stops when the decision
    threshold is met, no affordable investigation gains enough, or the budget can't afford any."""
    p = policy or ResearchPolicy()
    leader, conf = belief.leading

    if conf >= p.decision_threshold:
        return ResearchStep(ResearchAction.STOP, belief, decision=leader,
                            stop_reason=StopReason.DECISION_REACHED,
                            rationale=f"'{leader}' reached {conf:.2f} ≥ threshold {p.decision_threshold:.2f}")

    affordable = [inv for inv in investigations if inv.cost <= budget_remaining + 1e-9]
    if not affordable:
        return ResearchStep(ResearchAction.STOP, belief, decision=leader,
                            stop_reason=StopReason.BUDGET_EXHAUSTED,
                            rationale="no affordable investigation remains")

    scored = [(expected_information_gain(belief, inv), inv) for inv in affordable]
    best_gain, best = max(scored, key=lambda gi: _efficiency(gi[0], gi[1].cost, p.cost_weight))

    if best_gain < p.min_gain_bits:
        # is there anything left that COULD help but we can't afford, vs genuinely nothing informative?
        any_informative = any(expected_information_gain(belief, inv) >= p.min_gain_bits
                              for inv in investigations)
        reason = StopReason.BUDGET_EXHAUSTED if any_informative else StopReason.IRREDUCIBLY_AMBIGUOUS
        note = ("informative evidence exists but is unaffordable" if any_informative
                else "no remaining investigation can meaningfully reduce uncertainty")
        return ResearchStep(ResearchAction.STOP, belief, decision=leader, stop_reason=reason,
                            rationale=note)

    return ResearchStep(ResearchAction.INVESTIGATE, belief, expected_gain=best_gain, investigation=best,
                        decision=leader,
                        rationale=(f"'{best.id}' has the highest expected gain per cost "
                                   f"({best_gain:.2f} bits @ cost {best.cost:g})"))
