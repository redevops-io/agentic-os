"""Research-planner backtest — does information-gain planning reach correct conclusions with less
budget than naive strategies? (plan §10/§21).

On controlled tasks with a KNOWN ground-truth hypothesis and a fixed set of investigations (each with
a cost and a fixed outcome in that task's 'world'), we run the same sequential loop under four
selection strategies and compare. The stopping rule is SHARED across strategies (stop when the leading
hypothesis passes the decision threshold, the budget can't afford any remaining investigation, or a
step cap is hit), so the ONLY thing that differs is which evidence each strategy chooses to buy — the
fair test of selection quality.

  * info_gain   — highest expected information gain PER UNIT COST (the planner)
  * greedy_eig  — highest expected information gain, ignoring cost
  * random      — a random affordable remaining investigation
  * round_robin — remaining investigations in a fixed order

As with the other kernels this validates the planning LOGIC on a controlled benchmark; it is NOT
real-world research skill (that needs real questions, real evidence and real outcomes).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .research_planner import (
    Belief, Investigation, ResearchPolicy, expected_information_gain, update)


@dataclass(frozen=True)
class ResearchTask:
    hypotheses: Tuple[str, ...]
    ground_truth: str
    investigations: Tuple[Investigation, ...]
    fixed_outcomes: Dict[str, bool]        # this task's 'world': the fixed outcome of each investigation
    budget: float


# ── an INDEPENDENT task generator ─────────────────────────────────────────────────────
def _make_task(rng) -> ResearchTask:
    n = rng.choice([3, 4, 4, 5])
    hyps = tuple(f"H{i}" for i in range(n))
    truth = rng.choice(hyps)
    invs: List[Investigation] = []

    # one "signature" diagnostic per hypothesis: positive mainly when THAT hypothesis holds. Each comes
    # in a CHEAP and an EXPENSIVE variant of the SAME information — so cost-aware selection (gain/cost)
    # beats greedy (which buys the expensive one and burns the budget after one step).
    for h in hyps:
        sharp = rng.uniform(0.86, 0.96)
        low = rng.uniform(0.03, 0.12)
        likelihoods = {hh: (sharp if hh == h else low) for hh in hyps}
        invs.append(Investigation(id=f"sig-{h}-cheap", cost=rng.uniform(0.8, 1.4),
                                  pos_likelihoods=likelihoods, question=f"is it {h}? (cheap)"))
        invs.append(Investigation(id=f"sig-{h}-exp", cost=rng.uniform(7.0, 10.0),
                                  pos_likelihoods=likelihoods, question=f"is it {h}? (thorough)"))
    # cheap-but-useless distractors (≈0 information) — random / round-robin waste the budget here
    for j in range(rng.randint(4, 6)):
        base = rng.uniform(0.46, 0.54)
        invs.append(Investigation(
            id=f"noise-{j}", cost=rng.uniform(1.0, 1.8),
            pos_likelihoods={hh: base + rng.uniform(-0.02, 0.02) for hh in hyps}, question="weak signal"))

    rng.shuffle(invs)
    # a budget that affords ~3-4 cheap sharp investigations (enough to DECIDE if you pick well), or
    # just one expensive one — so which investigations you buy is decisive.
    budget = rng.uniform(4.0, 6.0)
    # the fixed 'world': each investigation's outcome given the ground truth
    outcomes = {inv.id: (rng.random() < inv.pos_likelihoods[truth]) for inv in invs}
    return ResearchTask(hyps, truth, tuple(invs), outcomes, budget)


def make_research_benchmark(seed: int = 7, n: int = 200) -> List[ResearchTask]:
    rng = random.Random(seed)
    return [_make_task(rng) for _ in range(n)]


# ── selection strategies (pick among affordable remaining; None ⇒ nothing to pick) ──
Selector = Callable[[Belief, Sequence[Investigation], "random.Random"], Optional[Investigation]]


def _select_info_gain(belief, remaining, _rng):
    return max(remaining, key=lambda inv: expected_information_gain(belief, inv) / max(inv.cost, 1e-9))


def _select_greedy(belief, remaining, _rng):
    return max(remaining, key=lambda inv: expected_information_gain(belief, inv))


def _select_random(belief, remaining, rng):
    return rng.choice(list(remaining))


def _select_round_robin(belief, remaining, _rng):
    return sorted(remaining, key=lambda inv: inv.id)[0]


STRATEGIES: Dict[str, Selector] = {
    "info_gain": _select_info_gain, "greedy_eig": _select_greedy,
    "random": _select_random, "round_robin": _select_round_robin,
}


@dataclass(frozen=True)
class SessionResult:
    correct: bool            # leading hypothesis == ground truth at stop
    decided: bool            # reached the decision threshold within budget
    cost_spent: float
    steps: int


def run_session(task: ResearchTask, selector: Selector, policy: Optional[ResearchPolicy] = None,
                *, rng_seed: int = 0) -> SessionResult:
    """Run the shared sequential loop under one selection strategy. Stopping is identical across
    strategies; only the selection differs."""
    p = policy or ResearchPolicy()
    rng = random.Random(rng_seed)
    belief = Belief.uniform(task.hypotheses)
    remaining = list(task.investigations)
    budget = task.budget
    steps = 0
    cap = len(task.investigations)
    while steps < cap:
        leader, conf = belief.leading
        if conf >= p.decision_threshold:
            break                                            # decided (shared stop)
        affordable = [inv for inv in remaining if inv.cost <= budget + 1e-9]
        if not affordable:
            break                                            # budget exhausted (shared stop)
        inv = selector(belief, affordable, rng)
        remaining.remove(inv)
        budget -= inv.cost
        steps += 1
        belief = update(belief, inv, task.fixed_outcomes[inv.id])
    leader, conf = belief.leading
    return SessionResult(correct=(leader == task.ground_truth), decided=(conf >= p.decision_threshold),
                         cost_spent=task.budget - budget, steps=steps)


# ── metrics + acceptance ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class StrategyMetrics:
    strategy: str
    correct_rate: float          # fraction ending on the true hypothesis
    decided_rate: float          # fraction that reached the decision threshold within budget
    mean_cost: float
    mean_steps: float


def run_acceptance(seed: int = 7, policy: Optional[ResearchPolicy] = None) -> Dict[str, StrategyMetrics]:
    tasks = make_research_benchmark(seed)
    out: Dict[str, StrategyMetrics] = {}
    for name, sel in STRATEGIES.items():
        results = [run_session(t, sel, policy) for t in tasks]
        n = len(results)
        out[name] = StrategyMetrics(
            strategy=name,
            correct_rate=sum(r.correct for r in results) / n,
            decided_rate=sum(r.decided for r in results) / n,
            mean_cost=sum(r.cost_spent for r in results) / n,
            mean_steps=sum(r.steps for r in results) / n)
    return out
