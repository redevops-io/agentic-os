"""Goal-driven growth agent (Growth Intelligence). Offline.

Goals rank the approval queue toward the objective (search visibility / lead gen); visitor intent becomes
approval-gated lead actions; goal progress is measured from the evidence. Governance unchanged — everything
consequential parks on approval.
"""
from __future__ import annotations

from agentic_os.content.interventions import plan_content_interventions
from agentic_os.content.search_signals import SearchObservation, SearchSignal, SignalType
from agentic_os.growth.goals import (
    Goal, GoalKind, goal_ranked, goals_advanced_by, measure_lead_generation, measure_search_visibility,
)
from agentic_os.growth.loop import growth_report
from agentic_os.growth.visitor_intelligence import (
    lead_intent_pages, plan_visitor_interventions,
)
from agentic_os.integrations.umami import AnalyticsObservation


def _content_signal(kind, page, conf=0.8):
    return SearchSignal(kind, query="agentic runtime", affected_pages=(page,), confidence=conf,
                        status="PROPOSE", proposed_action=f"do {kind.value}", evidence=(), site_id="redevops.io")


# ── goals + ranking ─────────────────────────────────────────────────────────────────────────────────────
def test_goal_aligned_actions_rank_first():
    viz = Goal("viz", GoalKind.SEARCH_VISIBILITY, site_id="redevops.io", target_value=5.0)
    ctr = plan_content_interventions([_content_signal(SignalType.CTR_OPPORTUNITY, "https://redevops.io/p")],
                                     source_app="content:redevops.io")
    assert ctr and goals_advanced_by(ctr[0], [viz]) == (viz,)          # CTR advances search visibility
    rows = goal_ranked(ctr, [viz])
    assert rows[0]["goal_aligned"] is True and rows[0]["goals"] == ["viz"]
    assert rows[0]["requires_approval"] is True                        # governance intact


def test_goal_scope_respects_site():
    other = Goal("g", GoalKind.SEARCH_VISIBILITY, site_id="quantify.club", target_value=5.0)
    d = plan_content_interventions([_content_signal(SignalType.NEAR_WIN, "https://redevops.io/p")],
                                   source_app="content:redevops.io")[0]
    assert goals_advanced_by(d, [other]) == ()                         # different site → not advanced


# ── visitor intelligence → leads ────────────────────────────────────────────────────────────────────────
def test_lead_intent_pages_and_interventions():
    obs = [AnalyticsObservation(page_url="https://redevops.io/pricing", pageviews=40, site_id="redevops.io"),
           AnalyticsObservation(page_url="https://redevops.io/blog/x", pageviews=200, site_id="redevops.io"),
           AnalyticsObservation(page_url="https://redevops.io/contact", pageviews=3, site_id="redevops.io")]
    sigs = lead_intent_pages(obs, min_views=10)
    kinds = {(s.subject, s.status) for s in sigs}
    assert ("https://redevops.io/pricing", "PROPOSE") in kinds         # intent page + traffic
    assert ("https://redevops.io/contact", "WATCH") in kinds            # intent page, thin traffic
    assert not any("/blog/" in s.subject for s in sigs)                 # not a commercial-intent path
    decisions = plan_visitor_interventions(sigs)
    assert len(decisions) == 1 and decisions[0].requires_approval is True   # only PROPOSE, parks on approval
    assert decisions[0].candidate.action_kind == "LEAD_INTENT"


def test_lead_goal_counts_leads_and_visitor_actions_align():
    lead = Goal("leads", GoalKind.LEAD_GENERATION, target_value=10.0)
    d = plan_visitor_interventions(lead_intent_pages(
        [AnalyticsObservation(page_url="https://redevops.io/demo", pageviews=50, site_id="redevops.io")]))[0]
    assert goals_advanced_by(d, [lead]) == (lead,)
    prog = measure_lead_generation(lead, 4)
    assert prog.current == 4.0 and prog.on_track is False


# ── goal progress (search) + full tick ──────────────────────────────────────────────────────────────────
def test_search_visibility_progress():
    goal = Goal("viz", GoalKind.SEARCH_VISIBILITY, target_queries=("agentic runtime",), target_value=5.0)
    obs = [SearchObservation(query="agentic runtime platform", page_url="p", impressions=200, position=4.0),
           SearchObservation(query="unrelated", page_url="q", impressions=50, position=30.0)]
    p = measure_search_visibility(goal, obs)
    assert p.current == 4.0 and p.on_track is True                      # only the matching query counts, pos 4 ≤ 5


def test_seo_goal_library_measurements():
    obs = [
        SearchObservation(query="agentic runtime", page_url="https://s/a", impressions=500, clicks=5,
                          ctr=0.01, position=4.0),                       # ranks, low CTR
        SearchObservation(query="agentic runtime", page_url="https://s/b", impressions=200, clicks=2,
                          ctr=0.01, position=7.0),                       # 2nd page for same subject → cannibalization
        SearchObservation(query="devops governance", page_url="https://s/c", impressions=80, clicks=6,
                          ctr=0.075, position=3.0),
    ]
    subjects = ("agentic runtime", "devops governance", "mission runtime")   # 3 target subjects
    from agentic_os.growth.goals import (
        measure_cannibalization, measure_content_coverage, measure_conversion, measure_ctr_improvement,
    )
    cov = measure_content_coverage(Goal("cov", GoalKind.CONTENT_COVERAGE, target_queries=subjects,
                                        target_value=0.9), obs)
    assert cov.current == round(2 / 3, 3) and cov.on_track is False       # 2 of 3 subjects covered

    can = measure_cannibalization(Goal("can", GoalKind.CANNIBALIZATION_RESOLUTION, target_queries=subjects,
                                       target_value=0.0), obs)
    assert can.current == 1.0 and can.on_track is False                  # "agentic runtime" has 2 pages

    ctr = measure_ctr_improvement(Goal("ctr", GoalKind.CTR_IMPROVEMENT, target_queries=subjects,
                                       target_value=0.05), obs)
    assert ctr.metric == "avg_ctr" and ctr.on_track is False             # avg CTR well under 5%

    conv = measure_conversion(Goal("conv", GoalKind.CONVERSION, target_value=0.02), conversions=6, sessions=200)
    assert conv.current == 0.03 and conv.on_track is True                # 3% ≥ 2% target


def test_new_goals_advance_the_right_actions():
    ctr_goal = Goal("g", GoalKind.CTR_IMPROVEMENT, target_value=0.05)
    d = plan_content_interventions([_content_signal(SignalType.CTR_OPPORTUNITY, "https://s/p")],
                                   source_app="content:s")[0]
    assert goals_advanced_by(d, [ctr_goal]) == (ctr_goal,)               # CTR_OPPORTUNITY advances CTR goal
    cov_goal = Goal("c", GoalKind.CONTENT_COVERAGE, target_value=0.9)
    d2 = plan_content_interventions([_content_signal(SignalType.CTR_OPPORTUNITY, "https://s/p")],
                                    source_app="content:s")[0]
    assert goals_advanced_by(d2, [cov_goal]) == ()                       # CTR action doesn't advance coverage


def test_growth_report_ranks_and_measures():
    viz = Goal("viz", GoalKind.SEARCH_VISIBILITY, target_queries=("agentic",), target_value=5.0)
    lead = Goal("leads", GoalKind.LEAD_GENERATION, target_value=10.0)
    content = plan_content_interventions([_content_signal(SignalType.CTR_OPPORTUNITY, "https://redevops.io/p")],
                                         source_app="content:redevops.io")
    visitor = plan_visitor_interventions(lead_intent_pages(
        [AnalyticsObservation(page_url="https://redevops.io/pricing", pageviews=50, site_id="redevops.io")]))
    obs = [SearchObservation(query="agentic os", page_url="p", impressions=300, position=3.0)]
    rep = growth_report(content + visitor, [viz, lead], search_observations=obs, leads=2)
    assert rep["summary"]["actions"] == 2 and rep["summary"]["goal_aligned"] == 2
    assert rep["summary"]["requires_approval"] == 2
    assert {g["goal_id"] for g in rep["goals"]} == {"viz", "leads"}
    assert any(g["goal_id"] == "viz" and g["on_track"] for g in rep["goals"])   # avg pos 3 ≤ 5
