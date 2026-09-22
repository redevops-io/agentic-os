"""Sidekick prose → a real Mission (planning), without letting prose authorize execution.

The sibling :mod:`.from_intent` guards the *sealed-intent → executable* path: a draft/unsealed intent is
refused, because planning it would execute a guess. This module is the front door for the OTHER, always-
valid entry — ``create_mission(goal: str, …)`` — used by Sidekick: a natural-language goal becomes a
Mission that is a PLAN. Creating a plan is not executing it; the mission's consequential steps stay
governed by approval gates, exactly as any mission does. So Sidekick can turn a sentence into durable
Mission state (replacing a scripted reply) while the "prose is not authorization" boundary holds.

The confirmed-intent upgrade seam is :meth:`SidekickMissionBridge.compile_confirmed`, which routes a
Discovery-sealed ``VerifiedIntent`` through ``create_mission_from_intent`` — the stronger path for when
the meaning has been confirmed rather than inferred from a sentence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence, Tuple


def _state_name(mission: Any) -> str:
    st = getattr(mission, "state", None)
    return getattr(st, "name", None) or getattr(st, "value", None) or str(st)


@dataclass(frozen=True)
class SidekickMission:
    """What Sidekick hands back: a real mission id + its state. ``needs_approval`` is True when the plan
    parked at a human gate (WAITING_HUMAN) — the governed boundary, surfaced honestly."""
    mission_id: str
    goal: str
    state: str
    constraints: Tuple[str, ...] = ()
    kind: str = ""
    needs_approval: bool = False

    def as_dict(self) -> dict:
        return {"mission_id": self.mission_id, "goal": self.goal, "state": self.state,
                "kind": self.kind, "constraints": list(self.constraints),
                "needs_approval": self.needs_approval}


# Goal recognizers → (kind, governance-hint constraints). These constraints are HINTS to the planner,
# never authorization: consequential steps still require approval regardless of what the sentence says.
_GOAL_RULES: Tuple[Tuple[Tuple[str, ...], str, Tuple[str, ...]], ...] = (
    (("overdue", "receivable", "collection", "unpaid", "follow up", "follow-up", "material accounts"),
     "receivables",
     ("consequential sends require approval", "payment is the verified outcome, not the send")),
    (("inspect", "deployment", "infrastructure", "security posture", "sentinel"),
     "deployment_inspection",
     ("read-only inspection", "any remediation requires approval")),
    (("stalled deal", "deal", "pipeline dropped", "why did we lose"),
     "deal_investigation", ("CRM writes require approval",)),
    (("escalation", "ticket", "root cause", "customer complaint"),
     "support_investigation", ("refunds/replies require approval",)),
)


def recognize(goal: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """Classify a goal into a known Mission kind + its governance-hint constraints, or None."""
    t = (goal or "").lower()
    for markers, kind, constraints in _GOAL_RULES:
        if any(m in t for m in markers):
            return kind, constraints
    return None


def is_actionable_goal(goal: str) -> bool:
    """A goal Sidekick should turn into a Mission (recognized job, or an explicit 'do this' request) —
    as opposed to a question, which stays a Q&A answer."""
    t = (goal or "").lower().strip()
    # a question is never an action, even when it mentions a recognizable job ("what is deployment…")
    if any(t.startswith(q) for q in ("what is", "what's", "how do", "how does", "why", "who", "when",
                                     "where", "is ", "are ", "can ", "does ", "explain", "tell me")):
        return False
    if recognize(t) is not None:
        return True
    return any(v in t for v in ("investigate", "find ", "collect", "reconcile", "draft", "review ",
                                "chase", "resolve", "handle", "run "))


@dataclass
class SidekickMissionBridge:
    """Turns a goal into a real Mission via the Mission Runtime (duck-typed on ``create_mission`` /
    ``create_mission_from_intent`` / ``run``). Deployment-neutral: any MissionRuntime satisfies it."""

    runtime: Any

    def compile(self, goal: str, *, actor: str = "", project_id: str = "",
                extra_constraints: Sequence[str] = (), run: bool = False) -> SidekickMission:
        rec = recognize(goal)
        kind = rec[0] if rec else "general"
        constraints = list(extra_constraints) + (list(rec[1]) if rec else [])
        if project_id:
            constraints.append(f"project:{project_id}")
        mission = self.runtime.create_mission(goal, constraints=constraints)
        state = _state_name(mission)
        if run:                                   # optionally drive planning; side-effecting steps still gate
            mission = self.runtime.run(mission.id)
            state = _state_name(mission)
        return SidekickMission(mission_id=mission.id, goal=goal, state=state,
                               constraints=tuple(constraints), kind=kind,
                               needs_approval=(state == "WAITING_HUMAN"))

    def compile_confirmed(self, intent: Any, *, policy_refs: Optional[list] = None) -> SidekickMission:
        """The stronger path: a Discovery-sealed VerifiedIntent → mission (meaning confirmed, not inferred)."""
        mission = self.runtime.create_mission_from_intent(intent, policy_refs=policy_refs)
        state = _state_name(mission)
        return SidekickMission(mission_id=mission.id, goal=getattr(intent, "objective", ""),
                               state=state, kind="confirmed", needs_approval=(state == "WAITING_HUMAN"))
