"""Close Plan compiler + Mission-DAG handoff (Phase 4, plan §14).

Pulls the whole chain together: a deal's verified condition states → methodology readiness → blockers →
hypotheses → scored candidate actions → an ordered, dependency-aware **Close Plan**, each step dispositioned by
the deployment's autonomy level. The plan is then compiled into a serializable Mission spec and handed to the
Mission Runtime — reusing the existing compile-to-mission seam (:mod:`agentic_os.mission.compiler`,
``discovery.mission_bridge``) rather than inventing an executor. The runtime is an injected Protocol so this
public module runs standalone (spec-only) with no enterprise dependency.

Nothing here decides to ACT on its own: a step's disposition comes from the autonomy level + the action's risk
tier, and the mission spec carries the approval-constraint tokens the runtime enforces.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Protocol, Tuple

from ..integrations.business.contracts import BusinessObject, Provenance
from ..priority_engine import Action, PriorityPolicy
from .autonomy import ActionDisposition, AutonomyLevel, disposition
from .blockers import infer_blockers
from .candidates import CandidateAction, ScoredAction, best_action, candidates_from_hypothesis
from .conditions import ConditionState
from .contracts import Deal
from .hypotheses import plan_hypotheses
from .methodology import ClosingMethodology, assess_methodology


@dataclass(frozen=True)
class PlannedStep:
    """One action in the close plan, with its disposition under the current autonomy level."""
    action: CandidateAction
    disposition: ActionDisposition
    priority_total: float
    rationale: str
    depends_on: Tuple[str, ...] = ()          # target conditions of prerequisite steps


@dataclass(frozen=True)
class DealClosePlan(BusinessObject):
    """An ordered, dependency-aware plan to move a deal toward close, derived from VERIFIED state."""
    KIND = "deal_closing.close_plan"
    deal_ref: str = ""
    methodology: str = ""
    derived_stage: str = ""
    autonomy: AutonomyLevel = AutonomyLevel.RECOMMEND
    steps: Tuple[PlannedStep, ...] = ()
    blocking_conditions: Tuple[str, ...] = ()
    verification_conditions: Tuple[str, ...] = ()
    ready_to_close: bool = False

    def steps_requiring_approval(self) -> Tuple[PlannedStep, ...]:
        return tuple(s for s in self.steps if s.disposition is ActionDisposition.REQUEST_APPROVAL)

    def executable_steps(self) -> Tuple[PlannedStep, ...]:
        return tuple(s for s in self.steps if s.disposition is ActionDisposition.EXECUTE)


def compile_close_plan(deal: Deal, methodology: ClosingMethodology, states: Mapping[str, ConditionState], *,
                       autonomy: AutonomyLevel = AutonomyLevel.RECOMMEND,
                       policy: Optional[PriorityPolicy] = None,
                       deal_value_cents: Optional[int] = None,
                       provider: str = "deal_closing") -> DealClosePlan:
    """Compile a deal + methodology + verified states into a dispositioned, ordered Close Plan. One best action
    per blocker (the one that beats do-nothing, else NO_ACTION); steps ordered by the methodology dependency
    graph (a step targeting a condition that depends on another step's target comes after it)."""
    deal_ref = deal.opportunity_ref or deal.account_ref or ""
    value = deal_value_cents if deal_value_cents is not None else deal.reported_amount_cents
    readiness = assess_methodology(methodology, states)
    blockers = infer_blockers(methodology, readiness, states, deal_ref=deal_ref, provider=provider)
    hyps = plan_hypotheses(blockers, provider=provider)

    steps: List[PlannedStep] = []
    for hyp in hyps:
        actions = candidates_from_hypothesis(hyp, deal_value_cents=value)
        chosen: ScoredAction = best_action(actions, policy=policy)
        disp = disposition(autonomy, chosen.action.authority())
        rationale = chosen.decision.rationale
        if chosen.decision.action is Action.ABSTAIN:
            rationale = "no action beats doing nothing yet — " + rationale
        steps.append(PlannedStep(action=chosen.action, disposition=disp,
                                 priority_total=round(chosen.total, 4), rationale=rationale))

    steps = _order_by_dependencies(steps, methodology)
    verification = tuple(dict.fromkeys(s.action.verification_condition for s in steps
                                       if s.action.verification_condition))
    return DealClosePlan(
        prov=Provenance(provider=provider),
        deal_ref=deal_ref, methodology=methodology.name, derived_stage=readiness.derived_stage,
        autonomy=autonomy, steps=tuple(steps), blocking_conditions=readiness.blocking_conditions,
        verification_conditions=verification, ready_to_close=readiness.ready_to_close)


def _order_by_dependencies(steps: List[PlannedStep], methodology: ClosingMethodology) -> List[PlannedStep]:
    """Order steps so that a step whose target condition depends (per the methodology) on another step's target
    comes after it. Stable topological-ish ordering; priority breaks ties. Also records ``depends_on``."""
    target_to_step = {s.action.target_condition: s for s in steps}
    enriched: List[PlannedStep] = []
    for s in steps:
        spec = methodology.spec(s.action.target_condition)
        deps = tuple(d for d in (spec.depends_on if spec else ()) if d in target_to_step)
        enriched.append(PlannedStep(action=s.action, disposition=s.disposition,
                                    priority_total=s.priority_total, rationale=s.rationale, depends_on=deps))

    ordered: List[PlannedStep] = []
    placed: set[str] = set()
    remaining = sorted(enriched, key=lambda x: x.priority_total, reverse=True)
    # greedily place steps whose deps are already placed; break cycles by priority
    while remaining:
        progress = False
        for s in list(remaining):
            if all(d in placed for d in s.depends_on):
                ordered.append(s)
                placed.add(s.action.target_condition)
                remaining.remove(s)
                progress = True
        if not progress:                      # dependency not among steps or a cycle — place best next
            s = remaining.pop(0)
            ordered.append(s)
            placed.add(s.action.target_condition)
    return ordered


# ── Mission-DAG handoff (mirrors discovery.mission_bridge) ──────────────────────────────────────────
@dataclass
class ClosePlanMissionSpec:
    """The serializable handoff from the Close Plan to the Mission Runtime. ``constraints`` are approval tokens
    the runtime enforces; ``steps`` list each executable/approval action with its target + verification."""
    goal: str
    deal_ref: str
    constraints: List[str] = field(default_factory=list)
    steps: List[dict] = field(default_factory=list)
    policy_refs: List[str] = field(default_factory=list)
    budget_usd: float = 5.0
    autonomy: str = ""


class MissionRuntimeLike(Protocol):
    def create_mission(self, goal: str, *, constraints: list | None = ...,
                       policy_refs: list | None = ..., budget: Any | None = ...,
                       template: str | None = ...) -> Any: ...
    def run(self, mission_id: str) -> Any: ...


def to_mission_spec(plan: DealClosePlan, *, grants: Optional[List[str]] = None,
                    budget_usd: float = 5.0) -> ClosePlanMissionSpec:
    """Translate a Close Plan into a Mission spec. Only PREPARE/REQUEST_APPROVAL/EXECUTE steps cross (RECORD and
    RECOMMEND never create mission work). Approval constraints come from the autonomy level: below
    POLICY_AUTHORIZED any side-effecting node stops for approval."""
    crossing = [s for s in plan.steps
                if s.disposition in (ActionDisposition.PREPARE, ActionDisposition.REQUEST_APPROVAL,
                                     ActionDisposition.EXECUTE)]
    if plan.autonomy >= AutonomyLevel.POLICY_AUTHORIZED:
        constraints: List[str] = ["approval:side_effecting"]   # even here, consequential writes gate
    elif plan.autonomy >= AutonomyLevel.APPROVAL_GATED:
        constraints = ["approval:side_effecting"]
    else:
        constraints = ["approval:all"]                         # observe/recommend/prepare: nothing auto-runs
    steps = [{"kind": s.action.kind.value, "target": s.action.target_condition,
              "authority": s.action.authority().name, "disposition": s.disposition.value,
              "verify": s.action.verification_condition} for s in crossing]
    goal = f"Advance deal {plan.deal_ref} ({plan.methodology}, stage {plan.derived_stage or 'unknown'})"
    return ClosePlanMissionSpec(goal=goal, deal_ref=plan.deal_ref, constraints=constraints, steps=steps,
                                policy_refs=list(grants) if grants is not None else ["*"],
                                budget_usd=budget_usd, autonomy=plan.autonomy.name)


def handoff(plan: DealClosePlan, runtime: Optional[MissionRuntimeLike] = None, *,
            grants: Optional[List[str]] = None, budget_usd: float = 5.0) -> Tuple[Optional[ClosePlanMissionSpec], Any]:
    """Return (spec, mission). spec is None when the plan has no crossing steps; mission is None in spec-only
    mode (no runtime attached). Never auto-runs — the runtime enforces the approval constraints."""
    spec = to_mission_spec(plan, grants=grants, budget_usd=budget_usd)
    if not spec.steps:
        return None, None
    if runtime is None:
        return spec, None
    mission = runtime.create_mission(spec.goal, constraints=spec.constraints,
                                     policy_refs=spec.policy_refs, budget=spec.budget_usd)
    return spec, mission


__all__ = [
    "PlannedStep", "DealClosePlan", "compile_close_plan",
    "ClosePlanMissionSpec", "MissionRuntimeLike", "to_mission_spec", "handoff",
]
