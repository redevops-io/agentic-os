"""The growth agent's single pass ("tick") — Growth Intelligence orchestration.

A long-running growth agent is just this tick on a schedule: **gather** every signal (content behavior + search,
visitor lead-intent, revenue leakage), **measure** progress toward the goals, and **rank** the resulting
governed decisions so the goal-advancing, approval-gated actions surface first. The tick itself is pure and
deterministic (`growth_report`); `gather_growth_decisions` is the live, self-skipping wiring that feeds it.

Governance is unchanged: every consequential action still parks on approval. The agent decides *what to bring
to the human, in what order, toward which goal* — it does not execute consequential actions on its own.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from agentic_os.growth.goals import Goal, goal_ranked, measure_goal


def _gsc_property(gsc, domain: str) -> str:
    """Resolve the GSC property matching a bare domain (domain property or URL-prefix). Empty if none."""
    props = set(gsc.sites())
    for cand in (f"sc-domain:{domain}", f"https://{domain}/", f"http://{domain}/",
                 f"https://{domain}", f"http://{domain}"):
        if cand in props:
            return cand
    return ""


def growth_report(decisions: List[Any], goals: List[Goal], *, search_observations: Optional[List[Any]] = None,
                  leads: int = 0, conversions: int = 0, sessions: int = 0,
                  engagement: float = 0.0) -> Dict[str, Any]:
    """The tick's output: goal progress + a goal-ranked approval queue + a summary. Pure over its inputs.
    Each goal is measured by its kind (SEO visibility/CTR/coverage/cannibalization, leads, conversion,
    engagement) from whatever evidence is supplied."""
    obs = list(search_observations or [])
    progress = [measure_goal(g, search_observations=obs, leads=leads, conversions=conversions,
                             sessions=sessions, engagement=engagement).as_dict() for g in goals]

    queue = goal_ranked(decisions, goals)
    return {
        "goals": progress,
        "queue": queue,
        "summary": {
            "actions": len(queue),
            "goal_aligned": sum(1 for r in queue if r["goal_aligned"]),
            "requires_approval": sum(1 for r in queue if r["requires_approval"]),
            "goals_on_track": sum(1 for p in progress if p["on_track"]),
            "goals_total": len(progress),
        },
    }


def gather_growth_decisions(umami, *, gsc=None, sites: Optional[List[Any]] = None, revenue_sources=None,
                            policy=None, days: int = 30, min_median: float = 20.0) -> Dict[str, List[Any]]:
    """Live, self-skipping gather of every governed decision across the sources, plus the GSC search
    observations used for goal progress. Returns {"decisions", "search_observations"}.

    - content (per site): Umami behavior + GSC search signals → content interventions
    - visitor (per site): Umami lead-intent pages → lead interventions
    - revenue (portfolio): composite leakage → revenue interventions
    Any source that isn't configured simply contributes nothing.
    """
    from agentic_os.content.behavior_signals import scan_behavior
    from agentic_os.content.interventions import plan_content_interventions
    from agentic_os.integrations.umami import collect_page_behavior
    from agentic_os.growth.visitor_intelligence import lead_intent_pages, plan_visitor_interventions

    decisions: List[Any] = []
    search_obs: List[Any] = []
    sites = sites or []

    for site in sites:
        scope = f"content:{site.site_id}"
        # GSC search signals + observations for goal progress
        search_sigs: List[Any] = []
        if gsc is not None:
            from agentic_os.integrations.gsc import (
                collect_search_observations, search_signals_from_observations,
            )
            prop = _gsc_property(gsc, site.domain)
            if prop:
                so = collect_search_observations(gsc, prop, days=min(days, 28), site_id=site.site_id)
                search_obs.extend(so)
                search_sigs = search_signals_from_observations(so)
        # Umami behavior + visitor lead-intent
        behavior_sigs: List[Any] = []
        visitor_sigs: List[Any] = []
        wid = umami.website_id_for(site.domain) if umami is not None else ""
        if wid:
            umami.website_id = wid
            obs = collect_page_behavior(umami, days=days, origin=site.resolved_origin(), site_id=site.site_id)
            behavior_sigs = scan_behavior(obs, min_median=min_median)
            visitor_sigs = lead_intent_pages(obs)
        decisions.extend(plan_content_interventions(list(behavior_sigs) + list(search_sigs), policy=policy,
                                                    source_app=scope))
        decisions.extend(plan_visitor_interventions(visitor_sigs, policy=policy,
                                                    source_app=f"growth:{site.site_id}"))

    if revenue_sources is not None:
        from agentic_os.intelligence.families import CompositeRevenueState
        from agentic_os.revenue.interventions import plan_leakage_interventions
        scope = "revenue"
        leaks = CompositeRevenueState(revenue_sources).leakages(scope) or []
        decisions.extend(plan_leakage_interventions(leaks, policy=policy, source_app="revenue"))

    return {"decisions": decisions, "search_observations": search_obs}
