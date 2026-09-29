"""Growth monitoring — the long-running agent's compare-to-last-tick brain (Growth Intelligence).

A long-running growth agent runs the tick on a schedule; each run is only useful next to the last one. This
computes the **diff** between two `growth_report`s: which goals slipped off-track (or recovered), which actions
are NEW since last time, which have CLEARED (resolved), and the current top goal-aligned work. That diff is
what an agent alerts on and what a human triages — the signal in the noise of a periodically-recomputed queue.

Pure + deterministic and storage-agnostic: the caller persists the last report (any store) and passes it back
in. Governance is unchanged — the monitor surfaces *what changed and what to approve*, it never executes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from agentic_os.growth.goals import Goal
from agentic_os.growth.loop import growth_report


@dataclass(frozen=True)
class GrowthDiff:
    slipped_goals: List[Dict[str, Any]] = field(default_factory=list)      # newly off-track this tick
    recovered_goals: List[Dict[str, Any]] = field(default_factory=list)    # newly on-track this tick
    new_actions: List[Dict[str, Any]] = field(default_factory=list)        # appeared since last tick
    cleared_actions: List[str] = field(default_factory=list)               # candidate_ids gone (resolved)
    persisting: int = 0                                                    # actions present both ticks
    top_goal_actions: List[Dict[str, Any]] = field(default_factory=list)   # current top goal-aligned work

    def as_dict(self) -> Dict[str, Any]:
        return {
            "slipped_goals": self.slipped_goals, "recovered_goals": self.recovered_goals,
            "new_actions": self.new_actions, "cleared_actions": self.cleared_actions,
            "persisting": self.persisting, "top_goal_actions": self.top_goal_actions,
            "has_alerts": bool(self.slipped_goals or self.new_actions),
        }


def diff_reports(previous: Optional[Dict[str, Any]], current: Dict[str, Any], *, top_n: int = 5) -> GrowthDiff:
    """Diff two growth reports. `previous` None = first run (everything is new; slippage is measured absolute)."""
    prev_q = (previous or {}).get("queue", [])
    prev_ids = {r.get("candidate_id") for r in prev_q}
    cur_q = current.get("queue", [])
    cur_ids = {r.get("candidate_id") for r in cur_q}

    new_actions = [r for r in cur_q if r.get("candidate_id") not in prev_ids]
    cleared = sorted(i for i in prev_ids - cur_ids if i)
    persisting = len(cur_ids & prev_ids)

    prev_track = {g.get("goal_id"): g.get("on_track") for g in (previous or {}).get("goals", [])}
    slipped, recovered = [], []
    for g in current.get("goals", []):
        was = prev_track.get(g.get("goal_id"))
        if not g.get("on_track") and (was is None or was):   # off-track now, and was on-track (or first seen)
            slipped.append(g)
        elif g.get("on_track") and was is False:
            recovered.append(g)

    top = [r for r in cur_q if r.get("goal_aligned")][:top_n]
    return GrowthDiff(slipped_goals=slipped, recovered_goals=recovered, new_actions=new_actions,
                      cleared_actions=cleared, persisting=persisting, top_goal_actions=top)


def monitor_tick(previous: Optional[Dict[str, Any]], decisions: List[Any], goals: List[Goal], *,
                 top_n: int = 5, **measure_inputs: Any) -> Tuple[Dict[str, Any], GrowthDiff]:
    """One monitored tick: build the current report and diff it against the previous one. Returns
    (report, diff) — persist `report` as the new baseline; act on `diff`. `measure_inputs` pass through to
    growth_report (search_observations, leads, conversions, sessions, engagement)."""
    report = growth_report(decisions, goals, **measure_inputs)
    return report, diff_reports(previous, report, top_n=top_n)
