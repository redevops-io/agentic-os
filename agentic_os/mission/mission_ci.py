"""Mission CI (v6 Phase 6.2) — test a mission before it is promoted.

`run_mission_ci` runs a template through the same pipeline a deploy would gate on and returns a
structured, pass/fail report over five checks:

  1. feasibility — compiles under the mission's grants (policy-prune leaves a permitted plan);
  2. budget     — the simulated projection stays within budget (nothing runs that can't afford to);
  3. run        — driven to a terminal state (auto-approving the declared gates) it reaches the
                  expected outcome;
  4. regression — the final world state contains the golden outcomes (no silent drift);
  5. replay     — a fresh runtime rehydrating the same event log reconstructs the same terminal
                  state (determinism holds).

`report.passed` is the promotion gate. The runtime factory takes an `EventStore` so checks 3 and 5
share one event log — replay is real, not simulated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .runtime import MissionRuntime
from .store import EventStore
from .types import Budget, MissionState

RuntimeFactory = Callable[[EventStore], MissionRuntime]


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CIReport:
    template: str
    passed: bool
    checks: list[Check] = field(default_factory=list)

    def summary(self) -> dict[str, bool]:
        return {c.name: c.passed for c in self.checks}

    def failures(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]


def _drive(rt: MissionRuntime, mid: str, approve: set[str]) -> None:
    """Run to a terminal state, auto-approving only the declared gate capabilities."""
    rt.run(mid)
    while rt._missions[mid].state == MissionState.WAITING_HUMAN:
        pending = rt.repo.pending_human(mid) or {}
        if pending.get("capability") not in approve:
            break  # a gate CI was not told to approve — stop (surfaces as a non-terminal 'run')
        rt.approve(mid, pending["node_id"], "approve")


def run_mission_ci(build_runtime: RuntimeFactory, *, goal: str, template: str, grants: list[str],
                   approve: list[str] | None = None, golden: dict | None = None,
                   budget: Budget | None = None) -> CIReport:
    approve_set = set(approve or [])
    golden = golden or {}
    checks: list[Check] = []
    store = EventStore()

    rt = build_runtime(store)
    m = rt.create_mission(goal, policy_refs=grants, template=template, budget=budget)

    # 1. feasibility — a permitted plan exists under the grants (+ budget)
    if m.state == MissionState.FAILED:
        checks.append(Check("feasibility", False, "no permitted plan (policy / capability / budget)"))
        return CIReport(template, False, checks)
    checks.append(Check("feasibility", True))

    # 2. budget — the active plan's projection is within budget
    plan = rt._plans.get(m.id)
    within = bool(plan and plan.projection and plan.projection.within_budget)
    checks.append(Check("budget", within, "" if within else "projection exceeds budget"))

    # 3. run — drive to terminal, auto-approving the declared gates
    _drive(rt, m.id, approve_set)
    reached = rt._missions[m.id].state
    want = golden.get("state", "succeeded")
    checks.append(Check("run", reached.value == want, f"reached {reached.value}, wanted {want}"))

    # 4. regression — golden outcomes present in world state
    world = set(rt._world(m.id).snapshot())
    missing = set(golden.get("world", [])) - world
    checks.append(Check("regression", not missing,
                        f"missing outcomes {sorted(missing)}" if missing else ""))

    # 5. replay — a fresh runtime on the same event log reconstructs the same terminal state
    rt2 = build_runtime(store)
    rt2.rehydrate(m.id)
    replayed = rt2._missions[m.id].state
    checks.append(Check("replay", replayed == reached,
                        "" if replayed == reached else f"rehydrated {replayed.value} != {reached.value}"))

    return CIReport(template, all(c.passed for c in checks), checks)
