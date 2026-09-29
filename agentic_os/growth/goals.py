"""Goals for the growth agent (Growth Intelligence — goal-driven prioritization).

The intelligence loops surface *what could be done*; a Goal says *what we're trying to achieve*, so the agent
can rank the approval queue by what actually advances the objective. Two goals matter here:

  * SEARCH_VISIBILITY — show up more often in Google for a set of target subjects (advanced by the content /
    search interventions: near-win, CTR, missing-page, cannibalization, leverage, …).
  * LEAD_GENERATION  — turn visitors into qualified leads (advanced by visitor-intent + re-engagement actions).

A Goal is scoped to a site (or all sites), carries a measurable target, and knows which action kinds advance
it. `goal_ranked` re-orders governed decisions so goal-advancing, approval-gated actions come first — every
action still parks on approval; goals change the *order*, never the governance. Pure + deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

# action kinds (content signal types / lead signal types) that advance each goal
_ADVANCES: Dict[str, frozenset] = {
    "SEARCH_VISIBILITY": frozenset({
        "NEAR_WIN", "CTR_OPPORTUNITY", "MISSING_PAGE", "CANNIBALIZATION", "TOPIC_EXPANSION",
        "EXISTING_INTENT", "HIGH_TRAFFIC_LEVERAGE", "UNDERPERFORMING_PAGE"}),
    "LEAD_GENERATION": frozenset({"LEAD_INTENT", "ACCOUNT_REENGAGEMENT_SIGNAL"}),
}


class GoalKind(str, Enum):
    SEARCH_VISIBILITY = "SEARCH_VISIBILITY"
    LEAD_GENERATION = "LEAD_GENERATION"


@dataclass(frozen=True)
class Goal:
    goal_id: str
    kind: GoalKind
    site_id: str = ""                       # "" = applies to every site
    description: str = ""
    target_queries: Tuple[str, ...] = ()    # SEARCH_VISIBILITY: the subjects we want to rank for
    target_value: float = 0.0               # SEARCH_VISIBILITY: target avg position; LEAD_GENERATION: leads/period
    metric: str = ""

    def advances(self, action_kind: str) -> bool:
        return action_kind in _ADVANCES.get(self.kind.value, frozenset())


@dataclass(frozen=True)
class GoalProgress:
    goal_id: str
    kind: GoalKind
    metric: str
    current: float
    target: float
    on_track: bool
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"goal_id": self.goal_id, "kind": self.kind.value, "metric": self.metric,
                "current": self.current, "target": self.target, "on_track": self.on_track,
                "detail": self.detail}


def _site_of(decision) -> str:
    """The site a decision is scoped to, from its candidate's source_app ('content:<site>' → '<site>')."""
    app = getattr(decision.candidate, "source_app", "") or ""
    return app.split(":", 1)[1] if ":" in app else ""


def goals_advanced_by(decision, goals: Iterable[Goal]) -> Tuple[Goal, ...]:
    """The goals a governed decision advances — its action kind is in the goal's advance-set and the goal's
    site scope matches (or is global)."""
    kind = getattr(decision.candidate, "action_kind", "") or ""
    site = _site_of(decision)
    return tuple(g for g in goals if g.advances(kind) and (not g.site_id or g.site_id == site))


# ── goal-aware ranking ────────────────────────────────────────────────────────────────────────────────────
def goal_ranked(decisions: List[Any], goals: List[Goal]) -> List[Dict[str, Any]]:
    """Tag each decision with the goals it advances and rank goal-aligned actions first, then by priority.
    Returns serializable rows (decision essentials + the goals served)."""
    rows: List[Dict[str, Any]] = []
    for d in decisions:
        served = goals_advanced_by(d, goals)
        c = d.candidate
        rows.append({
            "subject": c.subject,
            "action": d.action.value,
            "requires_approval": d.requires_approval,
            "priority": round(d.priority.total, 4),
            "proposed_action": c.proposed_action,
            "action_kind": c.action_kind,
            "required_capabilities": list(c.required_capabilities),
            "goals": [g.goal_id for g in served],
            "goal_aligned": bool(served),
            "candidate_id": c.candidate_id,
        })
    rows.sort(key=lambda r: (r["goal_aligned"], r["priority"]), reverse=True)
    return rows


# ── goal progress ─────────────────────────────────────────────────────────────────────────────────────────
def _matches_query(text: str, targets: Tuple[str, ...]) -> bool:
    t = (text or "").lower()
    return any(q.lower() in t for q in targets) if targets else True


def measure_search_visibility(goal: Goal, observations: Iterable[Any]) -> GoalProgress:
    """Progress toward ranking for the target subjects: average Google position (lower = better) + reach over
    the observations matching the target queries. on_track when avg position ≤ target_value."""
    rows = [o for o in observations if _matches_query(getattr(o, "query", ""), goal.target_queries)]
    impressions = sum(int(getattr(o, "impressions", 0) or 0) for o in rows)
    if not rows:
        return GoalProgress(goal.goal_id, goal.kind, "avg_position", 0.0, goal.target_value, False,
                            "no impressions yet for the target subjects")
    avg_pos = round(sum(float(getattr(o, "position", 0.0) or 0.0) for o in rows) / len(rows), 2)
    on_track = goal.target_value > 0 and avg_pos <= goal.target_value
    return GoalProgress(goal.goal_id, goal.kind, "avg_position", avg_pos, goal.target_value, on_track,
                        f"avg position {avg_pos} over {len(rows)} target query-pages, {impressions} impressions")


def measure_lead_generation(goal: Goal, leads: int) -> GoalProgress:
    """Progress toward the lead target: leads captured this period vs the target."""
    current = float(leads)
    on_track = current >= goal.target_value
    return GoalProgress(goal.goal_id, goal.kind, "leads", current, goal.target_value, on_track,
                        f"{leads} leads vs target {goal.target_value:g}")
