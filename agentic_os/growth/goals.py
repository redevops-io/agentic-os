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

# action kinds (content signal types / lead signal types) that advance each goal — the standard SEO/growth
# objectives, general-purpose (a deployment picks which goals it pursues; targets/subjects are config).
_ADVANCES: Dict[str, frozenset] = {
    # rank higher for target subjects
    "SEARCH_VISIBILITY": frozenset({
        "NEAR_WIN", "CTR_OPPORTUNITY", "MISSING_PAGE", "CANNIBALIZATION", "TOPIC_EXPANSION",
        "EXISTING_INTENT", "HIGH_TRAFFIC_LEVERAGE", "UNDERPERFORMING_PAGE"}),
    # earn more clicks from the rankings you already hold
    "CTR_IMPROVEMENT": frozenset({"CTR_OPPORTUNITY"}),
    # cover the intents you're missing (create the canonical pages)
    "CONTENT_COVERAGE": frozenset({"MISSING_PAGE", "TOPIC_EXPANSION", "EXISTING_INTENT", "NEAR_WIN"}),
    # stop pages competing with each other for the same intent
    "CANNIBALIZATION_RESOLUTION": frozenset({"CANNIBALIZATION"}),
    # turn visitors into leads
    "LEAD_GENERATION": frozenset({"LEAD_INTENT", "ACCOUNT_REENGAGEMENT_SIGNAL"}),
    # turn traffic into conversions (compound proven pages + capture intent)
    "CONVERSION": frozenset({"HIGH_TRAFFIC_LEVERAGE", "LEAD_INTENT"}),
    # get more out of the traffic you have (fix weak pages, compound strong ones)
    "ENGAGEMENT": frozenset({"HIGH_TRAFFIC_LEVERAGE", "UNDERPERFORMING_PAGE"}),
}


class GoalKind(str, Enum):
    SEARCH_VISIBILITY = "SEARCH_VISIBILITY"          # avg Google position over target subjects
    CTR_IMPROVEMENT = "CTR_IMPROVEMENT"              # click-through of ranking pages
    CONTENT_COVERAGE = "CONTENT_COVERAGE"            # share of target subjects with a ranking page
    CANNIBALIZATION_RESOLUTION = "CANNIBALIZATION_RESOLUTION"  # competing-page count (lower = better)
    LEAD_GENERATION = "LEAD_GENERATION"              # leads captured
    CONVERSION = "CONVERSION"                        # conversions from traffic
    ENGAGEMENT = "ENGAGEMENT"                        # engagement of existing traffic


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


def measure_ctr_improvement(goal: Goal, observations: Iterable[Any]) -> GoalProgress:
    """Average click-through of the ranking pages (position ≤ 10) for the target subjects. on_track when the
    average CTR meets target_value (a fraction, e.g. 0.05)."""
    rows = [o for o in observations
            if _matches_query(getattr(o, "query", ""), goal.target_queries)
            and 0 < float(getattr(o, "position", 0.0) or 0.0) <= 10.0]
    if not rows:
        return GoalProgress(goal.goal_id, goal.kind, "avg_ctr", 0.0, goal.target_value, False,
                            "no ranking pages yet for the target subjects")
    avg_ctr = round(sum(float(getattr(o, "ctr", 0.0) or 0.0) for o in rows) / len(rows), 4)
    return GoalProgress(goal.goal_id, goal.kind, "avg_ctr", avg_ctr, goal.target_value,
                        avg_ctr >= goal.target_value > 0,
                        f"avg CTR {avg_ctr:.1%} over {len(rows)} ranking query-pages")


def measure_content_coverage(goal: Goal, observations: Iterable[Any]) -> GoalProgress:
    """Share of the target subjects that already have a ranking page. on_track when coverage ≥ target_value
    (a 0..1 fraction). Needs target_queries to mean anything."""
    if not goal.target_queries:
        return GoalProgress(goal.goal_id, goal.kind, "coverage", 0.0, goal.target_value, False,
                            "no target subjects configured")
    seen = {q for q in goal.target_queries
            for o in observations if q.lower() in (getattr(o, "query", "") or "").lower()}
    coverage = round(len(seen) / len(goal.target_queries), 3)
    return GoalProgress(goal.goal_id, goal.kind, "coverage", coverage, goal.target_value,
                        coverage >= goal.target_value > 0,
                        f"{len(seen)}/{len(goal.target_queries)} target subjects have a ranking page")


def measure_cannibalization(goal: Goal, observations: Iterable[Any]) -> GoalProgress:
    """Number of subjects served by 2+ competing pages (lower is better). on_track when ≤ target_value."""
    by_q: Dict[str, set] = {}
    for o in observations:
        q = (getattr(o, "query", "") or "").lower()
        if q and _matches_query(q, goal.target_queries):
            by_q.setdefault(q, set()).add(getattr(o, "page_url", ""))
    competing = sum(1 for pages in by_q.values() if len(pages) >= 2)
    return GoalProgress(goal.goal_id, goal.kind, "cannibalized_subjects", float(competing), goal.target_value,
                        competing <= goal.target_value,
                        f"{competing} subject(s) with competing pages")


def measure_lead_generation(goal: Goal, leads: int) -> GoalProgress:
    """Progress toward the lead target: leads captured this period vs the target."""
    current = float(leads)
    return GoalProgress(goal.goal_id, goal.kind, "leads", current, goal.target_value,
                        current >= goal.target_value,
                        f"{leads} leads vs target {goal.target_value:g}")


def measure_conversion(goal: Goal, *, conversions: int, sessions: int = 0) -> GoalProgress:
    """Conversion rate (conversions / sessions) when sessions are known, else the conversion count.
    on_track when the measured value meets target_value."""
    if sessions > 0:
        rate = round(conversions / sessions, 4)
        return GoalProgress(goal.goal_id, goal.kind, "conversion_rate", rate, goal.target_value,
                            rate >= goal.target_value > 0, f"{conversions}/{sessions} = {rate:.1%}")
    return GoalProgress(goal.goal_id, goal.kind, "conversions", float(conversions), goal.target_value,
                        conversions >= goal.target_value, f"{conversions} conversions vs target {goal.target_value:g}")


def measure_engagement(goal: Goal, *, engagement: float) -> GoalProgress:
    """A supplied engagement metric (0..1 — e.g. 1 − bounce rate, or normalized dwell). on_track when ≥ target."""
    return GoalProgress(goal.goal_id, goal.kind, "engagement", round(float(engagement), 4), goal.target_value,
                        engagement >= goal.target_value > 0, f"engagement {engagement:.2f} vs target {goal.target_value:g}")


def measure_goal(goal: Goal, *, search_observations: Optional[Iterable[Any]] = None, leads: int = 0,
                 conversions: int = 0, sessions: int = 0, engagement: float = 0.0) -> GoalProgress:
    """Dispatch to the right measurement for a goal's kind, from whatever evidence is supplied."""
    obs = list(search_observations or [])
    k = goal.kind
    if k is GoalKind.SEARCH_VISIBILITY:
        return measure_search_visibility(goal, obs)
    if k is GoalKind.CTR_IMPROVEMENT:
        return measure_ctr_improvement(goal, obs)
    if k is GoalKind.CONTENT_COVERAGE:
        return measure_content_coverage(goal, obs)
    if k is GoalKind.CANNIBALIZATION_RESOLUTION:
        return measure_cannibalization(goal, obs)
    if k is GoalKind.LEAD_GENERATION:
        return measure_lead_generation(goal, leads)
    if k is GoalKind.CONVERSION:
        return measure_conversion(goal, conversions=conversions, sessions=sessions)
    return measure_engagement(goal, engagement=engagement)
