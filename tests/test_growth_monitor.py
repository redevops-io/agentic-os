"""Growth monitoring — diff between ticks (Growth Intelligence). Offline.

The long-running agent's brain: compare this tick to the last and surface what changed — goal slippage, new
actions to approve, cleared actions — so a periodically-recomputed queue produces alerts, not noise.
"""
from __future__ import annotations

from agentic_os.content.interventions import plan_content_interventions
from agentic_os.content.search_signals import SearchObservation, SearchSignal, SignalType
from agentic_os.growth.goals import Goal, GoalKind
from agentic_os.growth.monitor import diff_reports, monitor_tick


def _sig(kind, page, conf=0.8):
    return SearchSignal(kind, query="agentic", affected_pages=(page,), confidence=conf, status="PROPOSE",
                        proposed_action=f"do {kind.value}", evidence=(), site_id="s")


def _decisions(pages):
    sigs = [_sig(SignalType.CTR_OPPORTUNITY, p) for p in pages]
    return plan_content_interventions(sigs, source_app="content:s")


def _goal(target=5.0):
    return Goal("viz", GoalKind.SEARCH_VISIBILITY, site_id="s", target_queries=("agentic",), target_value=target)


def test_first_tick_everything_new_and_slippage_absolute():
    obs = [SearchObservation(query="agentic", page_url="p", impressions=100, position=30.0)]  # pos 30 > target 5
    report, diff = monitor_tick(None, _decisions(["https://s/a", "https://s/b"]), [_goal()],
                                search_observations=obs)
    assert len(diff.new_actions) == 2 and diff.cleared_actions == [] and diff.persisting == 0
    assert [g["goal_id"] for g in diff.slipped_goals] == ["viz"]      # off-track on first sight
    assert diff.as_dict()["has_alerts"] is True


def test_second_tick_detects_new_cleared_and_recovery():
    obs_bad = [SearchObservation(query="agentic", page_url="p", impressions=100, position=30.0)]
    prev, _ = monitor_tick(None, _decisions(["https://s/a", "https://s/b"]), [_goal()],
                           search_observations=obs_bad)
    # next tick: /a resolved, /c appeared; goal now on-track (avg position improved)
    obs_good = [SearchObservation(query="agentic", page_url="p", impressions=100, position=3.0)]
    _, diff = monitor_tick(prev, _decisions(["https://s/b", "https://s/c"]), [_goal()],
                           search_observations=obs_good)
    new_ids = {r["candidate_id"] for r in diff.new_actions}
    assert any("/c" in i for i in new_ids) and all("/a" not in i for i in new_ids)
    assert any("/a" in i for i in diff.cleared_actions) and diff.persisting == 1   # /b persists
    assert [g["goal_id"] for g in diff.recovered_goals] == ["viz"] and diff.slipped_goals == []


def test_no_change_no_alerts():
    obs = [SearchObservation(query="agentic", page_url="p", impressions=100, position=3.0)]  # on-track
    rep, _ = monitor_tick(None, _decisions(["https://s/a"]), [_goal()], search_observations=obs)
    _, diff = monitor_tick(rep, _decisions(["https://s/a"]), [_goal()], search_observations=obs)
    assert diff.new_actions == [] and diff.cleared_actions == [] and diff.slipped_goals == []
    assert diff.persisting == 1 and diff.as_dict()["has_alerts"] is False
