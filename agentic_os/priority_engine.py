"""Priority Engine — the shared proactive-intelligence spine (AGENTIC_APPS_PROACTIVE_INTELLIGENCE_PLAN
§1/§16/§17/§18/§19, Phase 0).

The plan's common pattern is: observe state → detect an opportunity/risk/anomaly/gap → generate
candidate *interventions* → estimate each one's value/confidence/urgency/cost/risk → **act, request
approval, defer, or abstain** → measure the outcome → learn. Rather than re-implement that in every
app, this is the reusable primitive. It deliberately lives inside the existing orchestration (a plain
module), NOT a new microservice, exactly as the plan prescribes.

Two ideas are load-bearing and are enforced here, not merely documented:

  * **Confidence is not expected value** (§18). Candidates are ranked by a transparent function of
    probability, expected upside/downside, urgency, reversibility, execution cost, human-attention
    cost, operational risk and information value — never by confidence alone.
  * **Doing nothing is a first-class candidate** (§17). :func:`decide` abstains whenever the evidence
    is too weak or the risk-adjusted value does not beat doing nothing, so a proactive agent has to
    justify acting rather than act by default (which is how proactive agents over-act).

Every intervention is routed through the same governance stance regardless of source app (§19): the
risk tier (reused from :mod:`agentic_os.agent_gateway.contracts`) decides whether it may auto-execute
or must park on a human approval gate. This module makes the *decision*; execution is delegated to the
existing GovernedEnvelope / Mission Runtime — it is not re-implemented here.

Scope / honesty: this is the decision logic and it is deterministic and fully tested. It does NOT
claim outcome-learning or real-world efficacy. :class:`OutcomeEvent` is the telemetry contract a
future learning loop will consume; the accuracy of the *detectors* that feed it is each detector's own
concern (Growth trend scoring is synthetic-validated, see trend_backtest; the Support signals are
deterministic rules). Nothing here fabricates a detector.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple, Union

from agentic_os.agent_gateway.contracts import ApprovalPolicy, RiskTier


# ── contracts (plan §1 InterventionCandidate, §16 Opportunity, §20 OutcomeEvent) ─────
@dataclass(frozen=True)
class Opportunity:
    """Something *discovered* (§16): 'Acme appears ready for a technical follow-up.' An opportunity is
    an observation, not yet an action — one opportunity can spawn several candidate interventions."""
    source_app: str
    subject: str
    kind: str                                    # taxonomy label, e.g. 'revenue', 'execution_risk'
    confidence: float                            # P(the reading of the world is correct), 0..1
    observation_refs: Tuple[str, ...] = ()       # evidence ids (never free-text guesses)
    detail: str = ""


@dataclass(frozen=True)
class InterventionCandidate:
    """Something the system *could do* about an opportunity (§1/§16). Values are the estimates the
    optimiser ranks; ``confidence`` (probability it's real) is kept SEPARATE from ``expected_value``
    (signed upside/downside magnitude) precisely because confidence is not expected value (§18)."""
    source_app: str
    subject: str
    proposed_action: str
    expected_value: float                        # net upside(+)/downside(-) magnitude if it's real
    confidence: float                            # 0..1, probability the opportunity is real
    urgency: float = 0.0                         # 0..1, how time-sensitive
    execution_cost: float = 0.0                  # 0..1, compute/tool/model cost to do it
    attention_cost: float = 0.0                  # 0..1, how much human attention it would consume
    risk_tier: RiskTier = RiskTier.READ          # operational risk / side-effect tier
    reversibility: float = 1.0                   # 0..1, 1 = fully reversible (softens the risk penalty)
    information_value: float = 0.0               # 0..1, value of the evidence acquiring this would yield
    required_capabilities: Tuple[str, ...] = ()
    observation_refs: Tuple[str, ...] = ()
    approval_policy: Optional[ApprovalPolicy] = None   # None ⇒ default_for(risk_tier)
    expiry: Optional[float] = None               # epoch after which the candidate is stale
    candidate_id: str = ""
    action_kind: str = ""                        # coarse action type (e.g. 'send_proposal', 'wait') —
                                                 # the key the outcome learner groups reward by

    def effective_approval_policy(self) -> ApprovalPolicy:
        return self.approval_policy or ApprovalPolicy.default_for(self.risk_tier)

    @property
    def learn_key(self) -> Tuple[str, str]:
        """The (source_app, action_kind) key the outcome learner attributes reward to."""
        return (self.source_app, self.action_kind or self.proposed_action[:24])


def do_nothing(subject: str, source_app: str = "") -> InterventionCandidate:
    """The always-present baseline candidate (§17). Zero value, zero cost, zero risk — every real
    candidate must beat it, which is what stops the system acting by default."""
    return InterventionCandidate(source_app=source_app, subject=subject, proposed_action="do nothing",
                                 expected_value=0.0, confidence=1.0, candidate_id="do-nothing")


class Action(enum.Enum):
    """The plan's four outcomes for a candidate (§1)."""
    ACT = "act"                       # low-risk / policy-allowed → execute automatically
    REQUEST_APPROVAL = "request_approval"  # park on a human approval gate
    DEFER = "defer"                   # worth doing but not now / beyond the attention budget
    ABSTAIN = "abstain"               # do nothing — evidence too weak or value doesn't beat nothing


# ── the scoring policy + explainable score (plan §18) ────────────────────────────────
@dataclass(frozen=True)
class PriorityPolicy:
    min_confidence: float = 0.55              # below ⇒ abstain (evidence too weak to act on)
    require_positive_value: bool = True       # do-nothing beats a non-positive risk-adjusted value
    attention_budget: int = 3                 # max items allowed to interrupt the human at once
    auto_execute_max_tier: RiskTier = RiskTier.BOUNDED_WRITE  # never auto-run above this tier
    allow_auto_bounded_writes: bool = False   # IF_POLICY → approval unless a deployment opts in
    w_urgency: float = 0.5
    w_execution_cost: float = 0.3
    w_attention_cost: float = 0.2
    w_risk: float = 0.25
    w_information: float = 0.3


@dataclass(frozen=True)
class PriorityScore:
    total: float
    risk_adjusted_value: float                # confidence * expected_value
    components: Mapping[str, float] = field(default_factory=dict)


def priority_score(c: InterventionCandidate, policy: Optional[PriorityPolicy] = None) -> PriorityScore:
    """A transparent priority: risk-adjusted value, boosted by urgency and information value, penalised
    by execution cost, human-attention cost and (reversibility-softened) operational risk. Every term
    is returned in ``components`` so a decision can always be explained."""
    p = policy or PriorityPolicy()
    risk_adjusted = c.confidence * c.expected_value
    urgency_boost = risk_adjusted * p.w_urgency * c.urgency
    info = p.w_information * c.information_value
    exec_pen = p.w_execution_cost * c.execution_cost
    attn_pen = p.w_attention_cost * c.attention_cost
    risk_pen = p.w_risk * (int(c.risk_tier) / int(RiskTier.CRITICAL)) * (1.0 - 0.5 * c.reversibility)
    total = risk_adjusted + urgency_boost + info - exec_pen - attn_pen - risk_pen
    return PriorityScore(
        total=total, risk_adjusted_value=risk_adjusted,
        components={"risk_adjusted_value": risk_adjusted, "urgency_boost": urgency_boost,
                    "information_value": info, "execution_cost": -exec_pen,
                    "attention_cost": -attn_pen, "risk_penalty": -risk_pen})


# ── the decision (plan §17 counterfactual + §19 governance) ──────────────────────────
@dataclass(frozen=True)
class InterventionDecision:
    candidate: InterventionCandidate
    action: Action
    priority: PriorityScore
    rationale: str
    requires_approval: bool = False


def decide(c: InterventionCandidate, policy: Optional[PriorityPolicy] = None) -> InterventionDecision:
    """Decide a single candidate's fate. Abstention (do-nothing) is checked FIRST — weak evidence or a
    value that doesn't beat doing nothing means we stay silent. A survivor is then routed by its risk
    tier: low-risk / policy-allowed auto-executes; anything consequential parks on a human gate."""
    p = policy or PriorityPolicy()
    score = priority_score(c, p)

    # §17: do-nothing is the baseline. Abstain rather than over-act.
    if c.confidence < p.min_confidence:
        return InterventionDecision(c, Action.ABSTAIN, score,
                                    f"confidence {c.confidence:.2f} below the {p.min_confidence:.2f} "
                                    "threshold — do nothing (or gather more evidence first)")
    if p.require_positive_value and score.risk_adjusted_value <= 0:
        return InterventionDecision(c, Action.ABSTAIN, score,
                                    "risk-adjusted value does not beat doing nothing")

    # §19: same governance stance for every source app — the risk tier decides the gate.
    pol = c.effective_approval_policy()
    if pol == ApprovalPolicy.AUTO:
        return InterventionDecision(c, Action.ACT, score, "low-risk, runs automatically")
    if pol == ApprovalPolicy.IF_POLICY:
        if p.allow_auto_bounded_writes and c.risk_tier <= p.auto_execute_max_tier:
            return InterventionDecision(c, Action.ACT, score, "bounded write, policy permits auto-execute")
        return InterventionDecision(c, Action.REQUEST_APPROVAL, score,
                                    "bounded write — policy requires approval", requires_approval=True)
    # REQUIRED / MANDATORY (consequential/critical: external comms, financial, destructive)
    return InterventionDecision(c, Action.REQUEST_APPROVAL, score,
                                f"{c.risk_tier.name.lower()} action requires human approval",
                                requires_approval=True)


# ── the cross-app "what needs me?" surface (plan §2/§6) ──────────────────────────────
@dataclass(frozen=True)
class AttentionSummary:
    """The global prioritised work surface (§2). ``surfaced`` are the few things worth interrupting
    the human for, in priority order; everything else was handled, deferred or judged too low-value."""
    surfaced: Tuple[InterventionDecision, ...]
    deferred: Tuple[InterventionDecision, ...]
    handled_automatically: Tuple[InterventionDecision, ...]
    abstained: int

    @property
    def other_count(self) -> int:
        return len(self.handled_automatically) + len(self.deferred) + self.abstained

    def as_dict(self) -> dict:
        """JSON-safe projection for the Projects API / Sidekick surface."""
        return {"summary": self.render(),
                "surfaced": [_decision_dict(d) for d in self.surfaced],
                "deferred": [_decision_dict(d) for d in self.deferred],
                "handled_automatically": len(self.handled_automatically),
                "abstained": self.abstained}

    def render(self) -> str:
        if not self.surfaced:
            head = "Nothing needs you right now."
        else:
            n = len(self.surfaced)
            lines = [f"{n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} you today.", ""]
            for i, d in enumerate(self.surfaced, 1):
                c = d.candidate
                lines.append(f"{i}. {c.source_app} — {c.subject}")
                lines.append(f"   {c.proposed_action}")
                lines.append(f"   {d.rationale} (confidence {c.confidence:.2f}, "
                             f"priority {d.priority.total:.2f}).")
            head = "\n".join(lines)
        if self.other_count:
            head += (f"\n\n{self.other_count} additional observation"
                     f"{'s were' if self.other_count != 1 else ' was'} handled automatically, "
                     "deferred, or judged too low-value to interrupt you.")
        return head


def _decision_dict(d: "InterventionDecision") -> dict:
    c = d.candidate
    return {"source_app": c.source_app, "subject": c.subject, "proposed_action": c.proposed_action,
            "action": d.action.value, "rationale": d.rationale, "confidence": round(c.confidence, 2),
            "priority": round(d.priority.total, 3), "risk_tier": c.risk_tier.name,
            "requires_approval": d.requires_approval, "candidate_id": c.candidate_id}


class PrioritySource(Protocol):
    """A source of proactive candidates — one app / detector. The cross-app surface consumes many of
    these (plan §2/§6). A plain ``Callable[[], Sequence[InterventionCandidate]]`` works too."""

    def collect(self) -> Sequence[InterventionCandidate]: ...


def collect_priorities(sources: Iterable[Union[PrioritySource, Callable[[], Sequence[InterventionCandidate]]]],
                       policy: Optional[PriorityPolicy] = None, *, now: Optional[float] = None) -> AttentionSummary:
    """Gather candidates from every registered source and reduce them to one prioritised surface —
    the 'consume Priority Engine candidates from the entire stack' wiring (§2). A source may be an
    object with ``.collect()`` or a zero-arg callable; either returns its candidates (``[]`` when it
    has nothing right now, which is the honest empty state for an unconfigured detector)."""
    candidates: List[InterventionCandidate] = []
    for s in sources:
        got = s.collect() if hasattr(s, "collect") else s()
        candidates.extend(got or ())
    return what_needs_me(candidates, policy, now=now)


def what_needs_me(candidates: Sequence[InterventionCandidate],
                  policy: Optional[PriorityPolicy] = None, *, now: Optional[float] = None) -> AttentionSummary:
    """Aggregate candidates from across the stack into one prioritised surface. Expired candidates and
    abstentions drop out; auto-executable ones are 'handled'; the highest-priority items needing a
    human fill the attention budget, and the rest that would need a human are deferred."""
    p = policy or PriorityPolicy()
    abstained = 0
    live: List[InterventionDecision] = []
    for c in candidates:
        if c.expiry is not None and now is not None and now >= c.expiry:
            abstained += 1                       # stale ⇒ treated as not worth surfacing
            continue
        d = decide(c, p)
        if d.action == Action.ABSTAIN:
            abstained += 1
        else:
            live.append(d)
    live.sort(key=lambda d: d.priority.total, reverse=True)
    handled = tuple(d for d in live if d.action == Action.ACT)
    need_human = [d for d in live if d.action == Action.REQUEST_APPROVAL]
    surfaced = tuple(need_human[: p.attention_budget])
    deferred = tuple(
        # anything needing a human beyond the budget is deferred (re-marked so its action is honest)
        InterventionDecision(d.candidate, Action.DEFER, d.priority,
                             "beyond today's attention budget — deferred", d.requires_approval)
        for d in need_human[p.attention_budget:])
    return AttentionSummary(surfaced=surfaced, deferred=deferred,
                            handled_automatically=handled, abstained=abstained)


# ── learning telemetry contract (plan §20) — recorded, not yet learned from ──────────
@dataclass(frozen=True)
class OutcomeEvent:
    """Common outcome telemetry every proactive capability emits (§20) — the record the learning loop
    consumes to change future selection. Rewards are MULTI-DIMENSIONAL (a reply and an unsubscribe are
    different axes, not one number), rewards are DELAYED (a conversion lands days after the action), and
    attribution is uncertain (did the action cause the outcome, or would it have happened anyway?)."""
    candidate_id: str
    source_app: str
    action: Optional[Action] = None          # governance action it ran under, when known
    accepted: Optional[bool] = None          # human accepted / rejected the recommendation
    edited: bool = False                     # human kept it but edited it
    observed_reward: Optional[float] = None  # scalar summary, when a single number is meaningful
    note: str = ""
    action_kind: str = ""                    # the action type, so the learner can group by it
    reward_dimensions: Mapping[str, float] = field(default_factory=dict)  # e.g. {"reply":1,"unsub":0}
    delay: float = 0.0                       # time between the action and the observed outcome
    attribution_confidence: float = 1.0      # 0..1, how confidently the outcome is attributed to the action

    def scalar_reward(self, weights: Optional[Mapping[str, float]] = None) -> float:
        """Collapse the reward to a single attribution-weighted number for ranking/learning. Uses the
        explicit ``observed_reward`` when set, else a (optionally weighted) sum of ``reward_dimensions``,
        scaled by ``attribution_confidence`` so weakly-attributed outcomes teach the model less."""
        if self.observed_reward is not None:
            base = self.observed_reward
        elif self.reward_dimensions:
            w = weights or {}
            base = sum((w.get(k, 1.0)) * v for k, v in self.reward_dimensions.items())
        else:
            base = 0.0
        return base * _clamp_local(self.attribution_confidence)


def record_outcome(decision: InterventionDecision, *, accepted: Optional[bool] = None,
                   edited: bool = False, observed_reward: Optional[float] = None, note: str = "",
                   reward_dimensions: Optional[Mapping[str, float]] = None, delay: float = 0.0,
                   attribution_confidence: float = 1.0) -> OutcomeEvent:
    return OutcomeEvent(candidate_id=decision.candidate.candidate_id,
                        source_app=decision.candidate.source_app, action=decision.action,
                        accepted=accepted, edited=edited, observed_reward=observed_reward, note=note,
                        action_kind=decision.candidate.learn_key[1],
                        reward_dimensions=dict(reward_dimensions or {}), delay=delay,
                        attribution_confidence=attribution_confidence)


# ── the common decision contract (§16/§17): one opportunity → many candidate actions → select ───
UtilityFn = Callable[[InterventionCandidate, float], float]   # (candidate, base priority) → utility


@dataclass(frozen=True)
class DecisionOpportunity:
    """Something discovered that admits SEVERAL possible responses (§16). A domain app produces these;
    the shared runtime selects and (later) learns. ``uncertainty`` is epistemic — how unsure we are
    about the opportunity as a whole — kept separate from each action's own confidence (§18)."""
    entity: str
    source_app: str
    candidate_actions: Tuple[InterventionCandidate, ...]
    evidence: Tuple[str, ...] = ()
    constraints: Mapping[str, Any] = field(default_factory=dict)   # e.g. {"max_risk_tier": RiskTier.CONSEQUENTIAL}
    uncertainty: float = 0.0
    expires_at: Optional[float] = None
    opportunity_id: str = ""


@dataclass(frozen=True)
class SelectedAction:
    """The runtime's choice among an opportunity's candidate actions — explainable by construction:
    it carries the governance decision, the expected utility, and the runner-up alternatives."""
    opportunity_id: str
    action: InterventionCandidate
    decision: InterventionDecision
    expected_utility: float
    reason: str
    alternatives: Tuple[Tuple[str, float], ...] = ()


def _action_label(c: InterventionCandidate) -> str:
    return c.action_kind or (c.proposed_action[:24] if c.proposed_action else c.candidate_id or "action")


def select_action(opp: DecisionOpportunity, policy: Optional[PriorityPolicy] = None, *,
                  utility_fn: Optional[UtilityFn] = None, now: Optional[float] = None) -> SelectedAction:
    """Pick the highest-expected-utility action for an opportunity — with do-nothing always in the pool
    (§17) and every candidate still routed through governance (§19). ``utility_fn`` is the OPTIONAL
    learned adjustment (base priority → learned utility); with none, selection is the static priority.
    Learning changes WHICH action is favoured, never whether governance applies."""
    p = policy or PriorityPolicy()
    pool: List[InterventionCandidate] = list(opp.candidate_actions)
    if not any(c.candidate_id == "do-nothing" for c in pool):
        pool.append(do_nothing(opp.entity, opp.source_app))          # do-nothing is first-class
    max_tier = opp.constraints.get("max_risk_tier")

    def utility(c: InterventionCandidate) -> float:
        base = priority_score(c, p).total
        return base if utility_fn is None else utility_fn(c, base)

    scored: List[Tuple[float, InterventionCandidate]] = []
    for c in pool:
        if (max_tier is not None and c.candidate_id != "do-nothing"
                and int(c.risk_tier) > int(max_tier)):
            continue                                                 # constraint filters out too-risky actions
        scored.append((utility(c), c))
    scored.sort(key=lambda uc: (uc[0], uc[1].candidate_id), reverse=True)   # deterministic tie-break
    best_u, best = scored[0]
    decision = decide(best, p)
    alts = tuple((_action_label(c), round(u, 3)) for u, c in scored[:4])
    learned = "" if utility_fn is None else " (learned-adjusted)"
    reason = (f"chose '{_action_label(best)}' — highest expected utility {best_u:.3f}{learned}; "
              f"{decision.rationale}")
    return SelectedAction(opp.opportunity_id, best, decision, best_u, reason, alts)


@dataclass
class OutcomeLog:
    """The append-only record of what was selected and what happened — the substrate the learning loop
    reads. In-memory here; a deployment persists it. Shared, durable telemetry, not per-run state."""
    events: List[OutcomeEvent] = field(default_factory=list)

    def record(self, ev: OutcomeEvent) -> OutcomeEvent:
        self.events.append(ev)
        return ev

    def for_key(self, source_app: str, action_kind: str) -> List[OutcomeEvent]:
        return [e for e in self.events if e.source_app == source_app and e.action_kind == action_kind]


# ── adapters: turn ALREADY-SHIPPED deterministic signals into candidates ─────────────
def from_trend_report(report, *, source_app: str = "growth") -> Optional[InterventionCandidate]:
    """Growth Opportunity Radar → an intervention candidate. Respects the trend kernel's own abstain:
    an abstained report yields no candidate. Value comes from the capitalization gap (open, uncrowded
    demand); urgency from where it sits in the lifecycle; confidence is the kernel's calibrated one.
    Publishing a campaign is an external action, so it parks on approval (§19)."""
    if getattr(report, "abstained", False):
        return None
    lifecycle = getattr(getattr(report, "lifecycle", None), "value", "")
    urgency = {"emerging": 0.9, "accelerating": 0.8, "breakout": 0.7, "mainstream": 0.4,
               "peaking": 0.2, "saturated": 0.05, "declining": 0.0, "latent": 0.3}.get(lifecycle, 0.5)
    return InterventionCandidate(
        source_app=source_app, subject=f"emerging topic: {report.entity}",
        proposed_action="Draft a campaign for the emerging topic and hold it for approval",
        expected_value=float(getattr(report, "capitalization_gap", 0.0)),
        confidence=float(getattr(report, "confidence", 0.0)), urgency=urgency,
        execution_cost=0.2, attention_cost=0.3, risk_tier=RiskTier.CONSEQUENTIAL, reversibility=1.0,
        required_capabilities=("growth.campaign.draft",),
        observation_refs=tuple(getattr(report, "reasons", ()) or ()),
        candidate_id=f"growth:{report.entity}")


def from_support_thread(thread_state, follow_up, lead_score, *,
                        source_app: str = "support") -> Optional[InterventionCandidate]:
    """Support follow-up → an intervention candidate. Respects the FollowUpPolicy: no candidate unless
    a follow-up is actually due (and never for an opted-out/resolved thread — the policy already
    refuses those). Value scales with the lead tier; urgency with thread risk. Sending an outbound
    message is consequential, so it parks on approval (§19)."""
    if not getattr(follow_up, "should_follow_up", False):
        return None
    tier_value = {"P0": 1.0, "P1": 0.65, "P2": 0.35, "P3": 0.15}.get(getattr(lead_score, "tier", "P3"), 0.15)
    risk = getattr(getattr(follow_up, "risk", None), "name", "")
    urgency = {"LOST": 0.95, "AT_RISK": 0.8, "COOLING": 0.5, "ENGAGED": 0.3}.get(risk, 0.5)
    n = getattr(follow_up, "follow_up_number", 1)
    return InterventionCandidate(
        source_app=source_app, subject=f"thread ({getattr(lead_score, 'tier', 'P?')} lead)",
        proposed_action=f"Send follow-up #{n} ({getattr(follow_up, 'reason', '')})",
        expected_value=tier_value,
        confidence=0.9,                          # the follow-up being *due* is deterministic
        urgency=urgency, execution_cost=0.1, attention_cost=0.2,
        risk_tier=RiskTier.CONSEQUENTIAL, reversibility=0.3,
        required_capabilities=("support.message.send",),
        candidate_id=f"support:{id(thread_state):x}:{n}")


def from_risk_report(report, *, source_app: str = "projects") -> Optional[InterventionCandidate]:
    """Projects Execution Risk Radar → an intervention candidate. Respects the risk kernel's abstain:
    an abstained report (thin evidence / below threshold / already complete) yields no candidate.
    Value reflects the calibrated slip probability (the schedule risk a mitigation would reduce);
    urgency follows the risk level. Approving a mitigation changes the plan, so it parks on approval
    (§19; plan §2 'Approval required for proposed mitigation')."""
    if getattr(report, "abstained", False):
        return None
    level = getattr(getattr(report, "risk_level", None), "name", "")
    urgency = {"CRITICAL": 0.9, "ELEVATED": 0.7, "EMERGING": 0.5}.get(level, 0.5)
    lo, hi = getattr(report, "slip_window_days", (0.0, 0.0))
    window = f"~{lo:g}–{hi:g}d slip" if hi > 0 else "slip risk"
    return InterventionCandidate(
        source_app=source_app, subject=f"milestone at risk: {report.milestone}",
        proposed_action=(f"Review the emerging execution risk ({window}) — "
                         f"{getattr(report, 'primary_cause', '')} — and approve a mitigation"),
        expected_value=float(getattr(report, "confidence", 0.0)),
        confidence=float(getattr(report, "confidence", 0.0)), urgency=urgency,
        execution_cost=0.15, attention_cost=0.3, risk_tier=RiskTier.CONSEQUENTIAL, reversibility=0.6,
        required_capabilities=("projects.mitigation.propose",),
        candidate_id=f"projects:{report.milestone}")


def from_research_plan(step, *, source_app: str = "research", question: str = "") -> Optional[InterventionCandidate]:
    """Research Information-Gain Planner → an intervention candidate. Only an INVESTIGATE step yields
    one (a STOP is a conclusion, not an action). Gathering evidence is low-risk, so it is a READ-tier
    candidate that can run automatically — this is the plan's 'acquire evidence' as a first-class move
    (§17). Its ``information_value`` carries the expected bits of uncertainty reduction."""
    action = getattr(getattr(step, "action", None), "value", "")
    inv = getattr(step, "investigation", None)
    if action != "investigate" or inv is None:
        return None
    gain = float(getattr(step, "expected_gain", 0.0))
    info = _clamp_local(gain)                      # bits → 0..1 information value (≥1 bit ⇒ maxed)
    q = question or getattr(inv, "question", "") or step.decision or "the open question"
    return InterventionCandidate(
        source_app=source_app, subject=f"open question: {q}",
        proposed_action=f"Run investigation '{inv.id}' to reduce uncertainty ({gain:.2f} bits)",
        expected_value=info, confidence=0.8,       # a computed information gain is a reliable estimate
        urgency=0.3, execution_cost=_clamp_local(getattr(inv, "cost", 0.0) / 10.0),
        risk_tier=RiskTier.READ, information_value=info,
        required_capabilities=("research.investigate",), candidate_id=f"research:{inv.id}")


def _clamp_local(x: float) -> float:
    return max(0.0, min(1.0, x))


def from_frontier_choice(choice, *, source_app: str = "learning", subject_label: str = "") -> Optional[InterventionCandidate]:
    """Knowledge Frontier → an intervention candidate. A TEACH/REVIEW/ASSESS choice becomes a low-risk
    (READ-tier) candidate — recommending what to learn/teach/probe next is safe and can run
    automatically; a STOP yields none. ``information_value`` carries the frontier priority."""
    action = getattr(getattr(choice, "action", None), "value", "")
    cid = getattr(choice, "concept_id", None)
    if action not in ("teach", "review", "assess") or cid is None:
        return None
    val = _clamp_local(float(getattr(choice, "priority", 0.0)))
    who = f" for {subject_label}" if subject_label else ""
    return InterventionCandidate(
        source_app=source_app, subject=f"next concept{who}: {cid}",
        proposed_action=getattr(choice, "rationale", "") or f"{action.title()} '{cid}'",
        expected_value=val, confidence=0.8, urgency=0.3, execution_cost=0.1,
        risk_tier=RiskTier.READ, information_value=val,
        required_capabilities=("learning.advance",), candidate_id=f"learning:{cid}")


def _demo() -> str:
    """A runnable end-to-end example (real engine, EXAMPLE data). ``python -m agentic_os.priority_engine``."""
    hot = InterventionCandidate(
        source_app="CRM", subject="Acme", proposed_action="Send the technical deployment proposal",
        expected_value=0.9, confidence=0.86, urgency=0.73, risk_tier=RiskTier.CONSEQUENTIAL,
        reversibility=0.4, candidate_id="crm:acme")
    risk = InterventionCandidate(
        source_app="Projects", subject="Production pilot", proposed_action="Approve proposed mitigation",
        expected_value=0.8, confidence=0.84, urgency=0.9, risk_tier=RiskTier.CONSEQUENTIAL,
        reversibility=0.5, candidate_id="projects:pilot")
    draft = InterventionCandidate(
        source_app="Growth", subject="emerging topic: agentic RAG", expected_value=0.6, confidence=0.78,
        urgency=0.85, proposed_action="Draft a campaign and hold it for approval",
        risk_tier=RiskTier.CONSEQUENTIAL, candidate_id="growth:agentic-rag")
    auto = InterventionCandidate(
        source_app="Knowledge", subject="stale KB article", proposed_action="Draft a corrected article",
        expected_value=0.4, confidence=0.7, urgency=0.3, risk_tier=RiskTier.READ, candidate_id="kb:stale")
    weak = InterventionCandidate(
        source_app="CRM", subject="MaybeCorp", proposed_action="Reach out", expected_value=0.5,
        confidence=0.3, risk_tier=RiskTier.CONSEQUENTIAL, candidate_id="crm:maybe")
    return what_needs_me([hot, risk, draft, auto, weak]).render()


if __name__ == "__main__":     # pragma: no cover
    print(_demo())
