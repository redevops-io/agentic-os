"""Service Fulfillment — feasibility + plan + lifecycle transitions (plan §18, P7)."""
from __future__ import annotations

from agentic_os.commercial import (
    ServiceRequirement, ServiceResource, assess_service_feasibility, plan_service, service_expected_transitions,
)


def _resources():
    return [ServiceResource("eng_a", skills=("install", "config"), capacity=1),
            ServiceResource("eng_b", skills=("install",), capacity=2)]


def test_feasible_when_skills_and_capacity_available():
    feas = assess_service_feasibility([ServiceRequirement("install", 2), ServiceRequirement("config", 1)],
                                      _resources(), required_by=1000)
    assert feas.feasible and feas.missing_skills == ()
    assert len(feas.matched) == 3


def test_infeasible_reports_missing_skill():
    feas = assess_service_feasibility([ServiceRequirement("welding", 1)], _resources())
    assert not feas.feasible and feas.missing_skills == ("welding",)


def test_infeasible_when_over_capacity():
    feas = assess_service_feasibility([ServiceRequirement("install", 4)], _resources())
    assert not feas.feasible and "install" in feas.missing_skills   # only 3 install-capacity


def test_plan_service_sets_feasible_and_risks():
    ok = plan_service("acme-impl", "onboarding", [ServiceRequirement("install", 1)], _resources(), required_by=1000)
    assert ok.feasible and ok.risks == () and ok.assigned
    bad = plan_service("acme-impl", "onboarding", [ServiceRequirement("welding", 1)], _resources())
    assert not bad.feasible and bad.risks


def test_service_lifecycle_transitions():
    plan = plan_service("acme-impl", "onboarding", [ServiceRequirement("install", 1)], _resources())
    ts = service_expected_transitions(plan)
    assert [t.expected_to_state for t in ts] == ["scheduled", "in_progress", "completed", "verified"]
