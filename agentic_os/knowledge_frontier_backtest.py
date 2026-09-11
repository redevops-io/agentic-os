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
        new[cid] = Mastery(prob=min(1.0, m.prob + gain * (1.0 - m.prob)), exposed=True, staleness=0.0, assessed=True)
    elif m.misconception:                        # a review after a misconception clears it slowly
        new[cid] = Mastery(prob=m.prob, exposed=True, misconception=rng.random() > 0.5, staleness=0.0, assessed=True)
    else:                                        # prereqs missing: little learned, may seed a misconception
        new[cid] = Mastery(prob=min(0.4, m.prob + rng.uniform(0.0, 0.1)), exposed=True,
                           misconception=(rng.random() < 0.4), staleness=0.0, assessed=True)
    for other, om in new.items():                # retention decay for everything else
        if other != cid and om.prob > 0:
            new[other] = Mastery(prob=om.prob, exposed=om.exposed, misconception=om.misconception,
                                 staleness=om.staleness + 1.0, assessed=om.assessed)
    return new


def _review(state: Dict[str, Mastery], cid: str) -> Dict[str, Mastery]:
    new = dict(state)
    m = new.get(cid, Mastery())
    new[cid] = Mastery(prob=min(1.0, m.prob + 0.1), exposed=True, misconception=False, staleness=0.0, assessed=True)
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
        if action == FrontierAction.ASSESS:                # doesn't arise in this scenario; be safe
            m = state.get(cid, Mastery())
            state = {**state, cid: Mastery(m.prob, True, m.misconception, m.staleness, True)}
            continue
        if not prerequisites_met(task.graph, state, cid, threshold=0.7):
            wasted += 1
        state = _teach(task.graph, state, cid, rng)
    return _weighted_mastery(task.graph, state), wasted


# ── ASSESS scenario: an experienced entity that MAY already know some concepts ───────
# The value of ASSESS: our prior belief is unreliable (the entity might already know a concept), so
# PROBING (cheap — a quick check) before TEACHING (expensive — a whole lesson) avoids sinking teaching
# effort into what's already known. We track hidden TRUE competence separately from our belief: assess
# only updates the belief (reveals the truth); teaching advances true competence. The metric is TRUE
# mastery reached within a fixed effort budget — no belief inflation on either side.
_ASSESS_COST = 1.0
_TEACH_COST = 3.0          # a lesson costs far more than a probe (the realistic asymmetry)
_TEACH_GAIN = 0.6          # from 0 competence, ~3 teaches to cross the 0.85 mastery bar
_MASTERY = 0.85


@dataclass(frozen=True)
class PreKnowledgeTask:
    graph: ConceptGraph
    known: frozenset            # concepts the entity ALREADY truly knows (hidden ground truth)
    effort_budget: float        # in effort units (assess is cheaper than teach)


def make_preknowledge_benchmark(seed: int = 7, n: int = 120) -> List[PreKnowledgeTask]:
    rng = random.Random(seed)
    tasks = []
    for _ in range(n):
        k = rng.randint(8, 12)
        concepts = {f"c{i}": Concept(f"c{i}") for i in range(k)}       # flat: isolate the ASSESS effect
        graph = ConceptGraph(concepts)
        known = frozenset(c for c in concepts if rng.random() < rng.uniform(0.5, 0.7))  # experienced entity
        unknown = sum(1 for c in concepts if c not in known)
        # a budget around what verify-first needs for full mastery (assess-all + teach the unknowns 3×),
        # so a teach-blindly strategy — which burns lessons on already-known concepts — falls short.
        budget = (_ASSESS_COST * k + _TEACH_COST * 3 * unknown) * rng.uniform(0.9, 1.0)
        tasks.append(PreKnowledgeTask(graph, known, budget))
    return tasks


def run_preknowledge_session(task: PreKnowledgeTask, *, enable_assess: bool, rng_seed: int = 0) -> Tuple[float, int]:
    """Returns (TRUE mastery fraction reached within the effort budget, wasted teaches on known concepts).
    Belief starts genuinely unsure (prob 0.5, unverified) for every concept — the entity's history is
    unknown. ASSESS reveals the truth into the belief (cheap, no competence change); TEACH advances true
    competence (expensive) and is wasted on a concept the entity already knew."""
    policy = FrontierPolicy(enable_assess=enable_assess)
    true_comp: Dict[str, float] = {c: (0.97 if c in task.known else 0.0) for c in task.graph.concepts}
    belief: Dict[str, Mastery] = {c: Mastery(prob=0.5, exposed=True, assessed=False) for c in task.graph.concepts}
    effort = task.effort_budget
    wasted = 0
    while effort > 1e-9:
        step = next_step(task.graph, belief, policy=policy)
        if step.action == FrontierAction.STOP:
            break
        cid = step.concept_id
        if step.action == FrontierAction.ASSESS:
            if effort < _ASSESS_COST:
                break
            effort -= _ASSESS_COST
            belief = {**belief, cid: Mastery(true_comp[cid], exposed=True, assessed=True)}  # reveal truth
        else:                                                # TEACH (expensive)
            if effort < _TEACH_COST:
                break
            effort -= _TEACH_COST
            if true_comp[cid] >= _MASTERY:
                wasted += 1                                  # taught a concept already truly mastered
            true_comp[cid] = min(1.0, true_comp[cid] + _TEACH_GAIN * (1.0 - true_comp[cid]))
            belief = {**belief, cid: Mastery(true_comp[cid], exposed=True, assessed=True)}   # teaching also reveals
    mastered = sum(1 for c in task.graph.concepts if true_comp[c] >= _MASTERY)
    return mastered / len(task.graph.concepts), wasted


@dataclass(frozen=True)
class AssessMetrics:
    variant: str
    mean_true_mastery: float
    mean_wasted_known_teaches: float


def run_assess_acceptance(seed: int = 7) -> Dict[str, AssessMetrics]:
    """Compare the frontier WITH ASSESS against the same frontier WITHOUT it (teach-on-belief-alone)
    on entities that may already know some concepts."""
    tasks = make_preknowledge_benchmark(seed)
    out: Dict[str, AssessMetrics] = {}
    for name, flag in (("assess_on", True), ("assess_off", False)):
        results = [run_preknowledge_session(t, enable_assess=flag, rng_seed=2000 + i)
                   for i, t in enumerate(tasks)]
        n = len(results)
        out[name] = AssessMetrics(name, sum(r[0] for r in results) / n, sum(r[1] for r in results) / n)
    return out


def run_acceptance(seed: int = 7) -> Dict[str, StrategyMetrics]:
    tasks = make_frontier_benchmark(seed)
    out: Dict[str, StrategyMetrics] = {}
    for name, sel in STRATEGIES.items():
        results = [run_session(t, sel, rng_seed=1000 + i) for i, t in enumerate(tasks)]
        n = len(results)
        out[name] = StrategyMetrics(name, sum(r[0] for r in results) / n, sum(r[1] for r in results) / n)
    return out
