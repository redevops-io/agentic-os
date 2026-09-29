"""Growth Intelligence — the goal-driven agent layer.

Goals (SEARCH_VISIBILITY, LEAD_GENERATION) turn the content + revenue + visitor signals into a goal-ranked,
approval-gated queue, and measure progress toward the objective. The long-running growth agent is `growth_report`
over freshly `gather`-ed decisions on a schedule; governance is unchanged (every consequential action parks on
approval).
"""
from .goals import (  # noqa: F401
    Goal, GoalKind, GoalProgress, goal_ranked, goals_advanced_by, measure_cannibalization,
    measure_content_coverage, measure_conversion, measure_ctr_improvement, measure_engagement,
    measure_goal, measure_lead_generation, measure_search_visibility,
)
from .visitor_intelligence import (  # noqa: F401
    VisitorSignal, from_visitor_signal, lead_intent_pages, plan_visitor_interventions,
)
from .loop import gather_growth_decisions, growth_report  # noqa: F401
