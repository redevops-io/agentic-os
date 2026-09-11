"""Knowledge Frontier — decide what to learn/teach next to maximise understanding
(AGENTIC_APPS_PROACTIVE_INTELLIGENCE_PLAN §12, generalised).

'Establish the map before traversing every branch': rather than push a long sequence of disconnected
facts, model understanding as a prerequisite graph of concepts, track each concept's mastery state, and
pick the next concept by structural importance, prerequisites, uncertainty, misconception risk and
retention need — optimising demonstrated mastery, not content consumed.

This is a DOMAIN-NEUTRAL stack primitive, not a tutoring product: a 'learner' is any entity acquiring a
body of concepts — a person being taught, a new hire onboarding, an agent building competence, a team
closing a skill gap. (It is unrelated to, and shares no code with, the separate learnerbot.ai product.)

    concept graph + mastery state → eligible concepts (prerequisites met) → priority per concept
      (importance · expected gain · uncertainty · misconception · retention) → TEACH / REVIEW / STOP

Deterministic and model-free. Whether this selection POLICY reaches mastery with fewer steps than naive
orderings is decided by :mod:`agentic_os.knowledge_frontier_backtest` on controlled learners — not
asserted here. As with the other kernels, that proves the planning LOGIC on a controlled benchmark, not
real-world pedagogy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


class ConceptState(Enum):
    NOT_ENCOUNTERED = "not_encountered"
    EXPOSED = "exposed"
    UNCERTAIN = "uncertain"
    PROBABLY_UNDERSTOOD = "probably_understood"
    MISCONCEPTION = "misconception"
    RETENTION_AT_RISK = "retention_at_risk"
    MASTERED = "mastered"


@dataclass(frozen=True)
class Concept:
    id: str
    prerequisites: Tuple[str, ...] = ()
    difficulty: float = 0.5                       # 0..1, informational (harder concepts cost more)


@dataclass(frozen=True)
class Mastery:
    """What we believe about a learner's grasp of one concept."""
    prob: float = 0.0                             # P(mastered), 0..1
    exposed: bool = False                         # has the concept been encountered at all?
    misconception: bool = False                   # an active wrong belief (worse than not knowing)
    staleness: float = 0.0                        # 'time' since last reinforced (retention decay proxy)


KnowledgeState = Mapping[str, Mastery]


# ── the concept graph + structural importance ────────────────────────────────────────
@dataclass
class ConceptGraph:
    concepts: Mapping[str, Concept]
    _importance: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        self._importance = self._compute_importance()

    def _transitive_dependents(self, cid: str) -> Set[str]:
        """Every concept that (transitively) depends on ``cid`` — its structural reach."""
        dependents: Set[str] = set()
        stack = [c.id for c in self.concepts.values() if cid in c.prerequisites]
        while stack:
            d = stack.pop()
            if d in dependents:
                continue
            dependents.add(d)
            stack.extend(c.id for c in self.concepts.values() if d in c.prerequisites)
        return dependents

    def _compute_importance(self) -> Dict[str, float]:
        n = len(self.concepts)
        if n <= 1:
            return {cid: 1.0 for cid in self.concepts}
        return {cid: len(self._transitive_dependents(cid)) / (n - 1) for cid in self.concepts}

    def importance(self, cid: str) -> float:
        """0..1: how foundational — the share of the body that (transitively) builds on this concept."""
        return self._importance.get(cid, 0.0)


# ── per-concept signals ────────────────────────────────────────────────────────────────
def _mastery(state: KnowledgeState, cid: str) -> Mastery:
    return state.get(cid, Mastery())


def prerequisites_met(graph: ConceptGraph, state: KnowledgeState, cid: str,
                      threshold: float = 0.7) -> bool:
    """True when every prerequisite is understood well enough to build on."""
    c = graph.concepts[cid]
    return all(_mastery(state, p).prob >= threshold and not _mastery(state, p).misconception
               for p in c.prerequisites)


def uncertainty(m: Mastery) -> float:
    """Highest when a concept is half-grasped (p≈0.5); low when clearly known or clearly not."""
    if not m.exposed:
        return 0.0
    return _clamp(1.0 - abs(2.0 * m.prob - 1.0))


def expected_gain(graph: ConceptGraph, cid: str, m: Mastery, mastery_threshold: float) -> float:
    """Room to grow, weighted by how foundational the concept is (advancing a foundation lifts more)."""
    room = _clamp(mastery_threshold - m.prob) / max(mastery_threshold, 1e-9)
    return _clamp(room * (0.4 + 0.6 * graph.importance(cid)))


def retention_need(m: Mastery, at_risk_staleness: float) -> float:
    """For a once-mastered concept, how overdue a review is (0 if fresh or never mastered)."""
    if m.prob < 0.6 or at_risk_staleness <= 0:
        return 0.0
    return _clamp(m.staleness / at_risk_staleness)


def concept_state(graph: ConceptGraph, state: KnowledgeState, cid: str, *,
                  mastery_threshold: float = 0.85, at_risk_staleness: float = 4.0) -> ConceptState:
    m = _mastery(state, cid)
    if m.misconception:
        return ConceptState.MISCONCEPTION
    if not m.exposed and m.prob <= 1e-9:
        return ConceptState.NOT_ENCOUNTERED
    if m.prob >= mastery_threshold:
        return (ConceptState.RETENTION_AT_RISK
                if retention_need(m, at_risk_staleness) >= 1.0 else ConceptState.MASTERED)
    if m.prob >= 0.6:
        return ConceptState.PROBABLY_UNDERSTOOD
    if uncertainty(m) >= 0.6:
        return ConceptState.UNCERTAIN
    return ConceptState.EXPOSED


# ── the frontier selection (plan §12) ─────────────────────────────────────────────────
class FrontierAction(Enum):
    TEACH = "teach"           # advance an eligible, not-yet-mastered concept
    REVIEW = "review"         # reinforce a mastered concept whose retention is at risk
    STOP = "stop"


class StopReason(Enum):
    OBJECTIVE_MET = "objective_met"             # all target concepts mastered
    ALL_MASTERED = "all_mastered"               # nothing left to learn
    BLOCKED_ON_PREREQS = "blocked_on_prereqs"   # unmastered concepts remain but all are prereq-blocked


@dataclass(frozen=True)
class FrontierPolicy:
    prereq_threshold: float = 0.7
    mastery_threshold: float = 0.85
    at_risk_staleness: float = 4.0
    w_importance: float = 0.4
    w_gain: float = 0.4
    w_uncertainty: float = 0.15
    w_misconception: float = 0.3      # a misconception is worth correcting before new material
    w_retention: float = 0.7          # reviewing an at-risk concept competes with teaching new ones


@dataclass(frozen=True)
class FrontierChoice:
    action: FrontierAction
    concept_id: Optional[str] = None
    state: Optional[ConceptState] = None
    priority: float = 0.0
    stop_reason: Optional[StopReason] = None
    rationale: str = ""


def _teach_priority(graph, state, cid, m, p: FrontierPolicy) -> float:
    return (p.w_importance * graph.importance(cid)
            + p.w_gain * expected_gain(graph, cid, m, p.mastery_threshold)
            + p.w_uncertainty * uncertainty(m)
            + p.w_misconception * (1.0 if m.misconception else 0.0))


def next_step(graph: ConceptGraph, state: KnowledgeState, *, objective: Optional[Set[str]] = None,
              policy: Optional[FrontierPolicy] = None) -> FrontierChoice:
    """Pick the single best next move: teach an eligible concept, review an at-risk one, or stop.
    Eligibility requires prerequisites met — which is what makes the frontier 'establish the map before
    traversing every branch' rather than waste effort on concepts the learner isn't ready for."""
    p = policy or FrontierPolicy()

    if objective and all(_mastery(state, c).prob >= p.mastery_threshold and not _mastery(state, c).misconception
                         for c in objective):
        return FrontierChoice(FrontierAction.STOP, stop_reason=StopReason.OBJECTIVE_MET,
                              rationale="every target concept is mastered")

    candidates: List[Tuple[float, str, FrontierAction]] = []
    unmastered_exists = blocked_exists = False
    for cid in graph.concepts:
        m = _mastery(state, cid)
        if m.prob >= p.mastery_threshold and not m.misconception:      # mastered
            need = retention_need(m, p.at_risk_staleness)
            if need >= 1.0:
                candidates.append((p.w_retention * need * (0.5 + 0.5 * graph.importance(cid)),
                                   cid, FrontierAction.REVIEW))
            continue
        unmastered_exists = True
        if prerequisites_met(graph, state, cid, p.prereq_threshold):
            candidates.append((_teach_priority(graph, state, cid, m, p), cid, FrontierAction.TEACH))
        else:
            blocked_exists = True

    if not candidates:
        if unmastered_exists and blocked_exists:
            return FrontierChoice(FrontierAction.STOP, stop_reason=StopReason.BLOCKED_ON_PREREQS,
                                  rationale="unmastered concepts remain but their prerequisites aren't met")
        return FrontierChoice(FrontierAction.STOP, stop_reason=StopReason.ALL_MASTERED,
                              rationale="nothing left to learn")

    prio, cid, action = max(candidates, key=lambda c: c[0])
    st = concept_state(graph, state, cid, mastery_threshold=p.mastery_threshold,
                       at_risk_staleness=p.at_risk_staleness)
    verb = "Review" if action == FrontierAction.REVIEW else "Advance"
    return FrontierChoice(action, concept_id=cid, state=st, priority=prio,
                          rationale=(f"{verb} '{cid}' (importance {graph.importance(cid):.2f}, "
                                     f"state {st.value})"))
