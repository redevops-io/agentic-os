"""Why is this mission not progressing? The watchdog must know before it acts.

`WAITING_RECONCILIATION` is an intentional parked state, not a stuck loop, and so is a mission waiting for a person's approval.
Treating them as stalled produces false alerts and, worse, would send a healthy waiting mission into automatic replanning, which for
an unknown outcome means trying the action again. The four situations are different and only two of them warrant intervention:

  an agent repeating an ineffective strategy       -> replan (the goal-loop controller's job)
  an agent waiting for a scheduled observation     -> nothing: the reconciliation probe is on its schedule
  an agent blocked on human authority              -> nothing automatic: a person is needed (approval, or an escalated reconciliation)
  an agent genuinely hung during execution         -> intervene

This module classifies the second, third and fourth from durable facts (the mission log and the reconciliation job table); the first needs
iteration history and belongs to the goal-loop controller. `watchdog_eligible` is True only for a hung RUNNING mission.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .fork_join import is_branch_node_id
from .types import MissionState

_TERMINAL = (MissionState.SUCCEEDED, MissionState.FAILED, MissionState.CANCELLED, MissionState.COMPENSATION_FAILED)


class Wait(str, Enum):
    ACTIVE = "active"                            # running and recently doing something
    HUNG = "hung"                                # RUNNING but nothing recorded for too long: the one state for intervention
    STALLED = "stalled"                          # a goal loop iterating without progress: the controller diagnoses and replans (not a watchdog case)
    WAITING_HUMAN = "waiting_human"              # blocked on a person's authority (an approval)
    WAITING_OBSERVATION = "waiting_observation"  # an unknown outcome is being probed on a schedule
    WAITING_ESCALATED = "waiting_escalated"      # an unknown outcome the supervisor could not resolve: a person must
    PAUSED = "paused"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class Assessment:
    kind: Wait
    detail: str = ""
    next_probe_at: Optional[float] = None
    attempts: int = 0

    @property
    def watchdog_eligible(self) -> bool:
        """May an automatic replan / intervention act on this mission? Only a genuinely hung one."""
        return self.kind is Wait.HUNG

    @property
    def needs_a_person(self) -> bool:
        return self.kind in (Wait.WAITING_HUMAN, Wait.WAITING_ESCALATED)


def classify(rt, mission_id: str, jobs=None, *, now: Optional[float] = None, hung_after_s: float = 900.0) -> Assessment:
    now = time.time() if now is None else now
    m = rt._missions[mission_id]
    if m.state in _TERMINAL:
        return Assessment(Wait.TERMINAL)
    pend = rt.repo.pending_reconciliation(mission_id)
    # a plan node's wait is "not stuck" if independent plan work could run; a fork branch's is the fork's, which has already run
    # everything it could before it parked (a branch awaiting reconciliation is never "independent work" of itself)
    independent = (rt._independent_work(mission_id, rt._plans[mission_id])
                   if pend and not any(is_branch_node_id(p.get("node_id", "")) for p in pend) else False)
    if pend and not independent:
        job = jobs.get(pend[0].get("lease_id", "")) if jobs is not None and pend[0].get("lease_id") else None
        if job is not None and job["state"] == "escalated":
            return Assessment(Wait.WAITING_ESCALATED, f"unresolved after {job['attempts']} probe(s): {job.get('escalation_reason') or ''}",
                              attempts=job["attempts"])
        if job is not None:
            return Assessment(Wait.WAITING_OBSERVATION, "probing the provider on a schedule", job["next_at"], job["attempts"])
        return Assessment(Wait.WAITING_OBSERVATION, "an unknown outcome awaits its first probe")
    if rt.repo.pending_human(mission_id) is not None:
        return Assessment(Wait.WAITING_HUMAN, "waiting for a person's approval")
    if m.state is MissionState.PAUSED:
        return Assessment(Wait.PAUSED)
    tl = rt.repo.timeline(mission_id)
    last = tl[-1]["ts"] if tl else now
    if m.state is MissionState.RUNNING and now - last >= hung_after_s:
        return Assessment(Wait.HUNG, f"running, but nothing has been recorded for {int(now - last)}s")
    return Assessment(Wait.ACTIVE)
