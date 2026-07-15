"""Two-stage policy in the Mission kernel — stage 1: a policy-scoped registry means forbidden
capabilities are never candidates (the planner can't plan over or leak their existence)."""
from __future__ import annotations

from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.registry import PolicyScopedRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import MissionState

FULL = ["billing:write", "support:write", "books:write", "compliance:write"]


def test_scoped_registry_hides_forbidden_capabilities():
    reg, _ = build_fleet()
    scoped = PolicyScopedRegistry(reg, ["billing:write"])   # only billing permitted
    names = {c.name for c in scoped.all()}
    assert "billing.create_subscription" in names
    assert "compliance.file_consent" not in names           # forbidden -> not a candidate
    assert scoped.get("compliance.file_consent") is None
    assert scoped.providing("consent_filed") == []
    assert all(c.name != "compliance.file_consent" for c, _ in scoped.discover("file consent", k=10))


def test_star_grant_sees_everything():
    reg, _ = build_fleet()
    assert len(PolicyScopedRegistry(reg, ["*"]).all()) == len(reg.all())


def test_mission_blocked_when_a_needed_capability_is_forbidden():
    reg, client = build_fleet()
    rt = MissionRuntime(reg, Executor(client))
    # missing compliance:write -> the consent step has no permitted provider
    m = rt.create_mission("Onboard a new customer", template="onboarding",
                          policy_refs=["billing:write", "support:write", "books:write"])
    assert m.state == MissionState.FAILED
    blocked = [e for e in rt.repo.timeline(m.id) if e["type"] == "MissionBlocked"]
    assert blocked and blocked[0]["payload"]["reason"] == "no_permitted_plan"


def test_mission_runs_with_full_grants():
    reg, client = build_fleet()
    rt = MissionRuntime(reg, Executor(client))
    m = rt.create_mission("Onboard a new customer", template="onboarding", policy_refs=FULL)
    assert m.state != MissionState.FAILED
    rt.run(m.id)
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    assert rt._missions[m.id].state == MissionState.SUCCEEDED
