"""Knowledge-frontier backtest — does frontier selection reach mastery with fewer teaching steps than
naive orderings? (plan §12/§21).

On controlled learners with a KNOWN concept graph and a fixed learning dynamic, we run the same
teaching loop under four selection strategies and compare how much understanding is reached within a
fixed budget of steps. The dynamic is the ground truth: teaching a concept whose prerequisites are met
advances it; teaching one whose prerequisites are NOT met is largely wasted and can seed a
misconception; mastered concepts slowly go stale (retention). So a strategy that respects prerequisites
and prioritises foundations should reach far more mastery per step than one that doesn't.

  * frontier       — the Knowledge Frontier policy (importance · prereqs · uncertainty · retention)
  * random         — a random not-yet-mastered concept (ignores prerequisites)
  * linear         — concepts in a fixed order (ignores prerequisites)
  * importance_only — highest structural importance, ignoring prerequisites and retention

As with the other kernels this validates the planning LOGIC on a controlled benchmark; it is NOT
real-world pedagogy.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .knowledge_frontier import (
    Concept, ConceptGraph, FrontierAction, FrontierPolicy, KnowledgeState, Mastery, next_step)


@dataclass(frozen=True)
class LearnerTask:
    graph: ConceptGraph
    order: Tuple[str, ...]          # a fixed concept order (for the 'linear' baseline & determinism)
    budget: int                     # number of teaching steps allowed


# ── an INDEPENDENT concept-graph generator (layered prerequisite DAG) ────────────────
def _make_task(rng) -> LearnerTask:
    layers = rng.randint(3, 4)
    width = rng.randint(3, 4)
    concepts: Dict[str, Concept] = {}
    prev_layer: List[str] = []
    order: List[str] = []
    for L in range(layers):
        this_layer: List[str] = []
        for w in range(width):
            cid = f"L{L}c{w}"
            # each concept depends on 1-2 concepts from the previous layer (none in layer 0)
            prereqs = tuple(rng.sample(prev_layer, min(len(prev_layer), rng.randint(1, 2)))) if prev_layer else ()
            concepts[cid] = Concept(id=cid, prerequisites=prereqs, difficulty=rng.uniform(0.3, 0.8))
            this_layer.append(cid); order.append(cid)
        prev_layer = this_layer
    graph = ConceptGraph(concepts)
    # budget affords roughly 60-80% of the concepts — so WHICH you teach (and not wasting steps on
    # prereq-blocked ones) decides how much mastery you reach.
    budget = max(1, int(len(concepts) * rng.uniform(0.6, 0.8)))
    rng.shuffle(order)
    return LearnerTask(graph, tuple(order), budget)


def make_frontier_benchmark(seed: int = 7, n: int = 120) -> List[LearnerTask]:
    rng = random.Random(seed)
    return [_make_task(rng) for _ in range(n)]


# ── the learning dynamic (ground truth) ──────────────────────────────────────────────
_MASTERY = 0.85


def _teach(graph: ConceptGraph, state: Dict[str, Mastery], cid: str, rng) -> Dict[str, Mastery]:
    """Advance a concept IF its prerequisites are met; otherwise mostly wasted, with a misconception
    risk. Also age every other concept slightly (retention decay)."""
    from .knowledge_frontier import prerequisites_met
    new = dict(state)
    m = new.get(cid, Mastery())
    ready = prerequisites_met(graph, state, cid, threshold=0.7)
    if ready and not m.misconception:
        gain = rng.uniform(0.45, 0.65)
        new[cid] = Mastery(prob=min(1.0, m.prob + gain * (1.0 - m.prob)), exposed=True, staleness=0.0)
    elif m.misconception:                        # a review after a misconception clears it slowly
        new[cid] = Mastery(prob=m.prob, exposed=True, misconception=rng.random() > 0.5, staleness=0.0)
    else:                                        # prereqs missing: little learned, may seed a misconception
        new[cid] = Mastery(prob=min(0.4, m.prob + rng.uniform(0.0, 0.1)), exposed=True,
                           misconception=(rng.random() < 0.4), staleness=0.0)
    for other, om in new.items():                # retention decay for everything else
        if other != cid and om.prob > 0:
            new[other] = Mastery(prob=om.prob, exposed=om.exposed, misconception=om.misconception,
                                 staleness=om.staleness + 1.0)
    return new


def _review(state: Dict[str, Mastery], cid: str) -> Dict[str, Mastery]:
    new = dict(state)
    m = new.get(cid, Mastery())
    new[cid] = Mastery(prob=min(1.0, m.prob + 0.1), exposed=True, misconception=False, staleness=0.0)
    return new


def _weighted_mastery(graph: ConceptGraph, state: KnowledgeState) -> float:
    """Fraction of importance-weighted understanding achieved (the objective: demonstrated mastery)."""
    total = sum(0.3 + 0.7 * graph.importance(c) for c in graph.concepts)
    got = sum((0.3 + 0.7 * graph.importance(c)) * min(1.0, state.get(c, Mastery()).prob)
              for c in graph.concepts)
    return got / total if total else 0.0


# ── selection strategies (pick a concept id, or None to stop) ────────────────────────
def _sel_frontier(task, state, rng):
    choice = next_step(task.graph, state, policy=FrontierPolicy())
    return (choice.concept_id, choice.action) if choice.action != FrontierAction.STOP else (None, None)


def _unmastered(task, state):
    return [c for c in task.graph.concepts if state.get(c, Mastery()).prob < _MASTERY]


def _sel_random(task, state, rng):
    rem = _unmastered(task, state)
    return (rng.choice(rem), FrontierAction.TEACH) if rem else (None, None)


def _sel_linear(task, state, rng):
    rem = [c for c in task.order if state.get(c, Mastery()).prob < _MASTERY]
    return (rem[0], FrontierAction.TEACH) if rem else (None, None)


def _sel_importance_only(task, state, rng):
    rem = _unmastered(task, state)
    if not rem:
        return (None, None)
    return (max(rem, key=lambda c: task.graph.importance(c)), FrontierAction.TEACH)


STRATEGIES: Dict[str, Callable] = {
    "frontier": _sel_frontier, "random": _sel_random, "linear": _sel_linear,
    "importance_only": _sel_importance_only,
}


@dataclass(frozen=True)
class StrategyMetrics:
    strategy: str
    mean_mastery: float          # importance-weighted understanding reached within budget
    mean_wasted_steps: float     # steps spent teaching a prereq-blocked concept (≈ no gain)


def run_session(task: LearnerTask, selector: Callable, *, rng_seed: int = 0) -> Tuple[float, int]:
    from .knowledge_frontier import prerequisites_met
    rng = random.Random(rng_seed)
    state: Dict[str, Mastery] = {}
    wasted = 0
    for _ in range(task.budget):
        cid, action = selector(task, state, rng)
        if cid is None:
            break
        if action == FrontierAction.REVIEW:
            state = _review(state, cid)
            continue
        if not prerequisites_met(task.graph, state, cid, threshold=0.7):
            wasted += 1
        state = _teach(task.graph, state, cid, rng)
    return _weighted_mastery(task.graph, state), wasted


def run_acceptance(seed: int = 7) -> Dict[str, StrategyMetrics]:
    tasks = make_frontier_benchmark(seed)
    out: Dict[str, StrategyMetrics] = {}
    for name, sel in STRATEGIES.items():
        results = [run_session(t, sel, rng_seed=1000 + i) for i, t in enumerate(tasks)]
        n = len(results)
        out[name] = StrategyMetrics(name, sum(r[0] for r in results) / n, sum(r[1] for r in results) / n)
    return out
