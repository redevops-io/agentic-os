"""v6 Phase 6.2 — Mission CI: the promotion gate over a mission template.

Uses the onboarding template + an SDK stand-in fleet; the runtime factory takes the shared
EventStore so the replay check is a real rehydrate of the same event log.
"""
from __future__ import annotations

from agentic_os.mission.executor import Executor
from agentic_os.mission.mission_ci import run_mission_ci
from agentic_os.mission.operator_sdk import Operator, LocalOperatorClient, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]
GOLDEN = {"state": "succeeded",
          "world": ["subscription", "onboarding_sent", "revenue_recorded", "consent_filed"]}


def _fleet():
    return {
        # reversible side-effects (undo present) as the real operators are — so the P8 policy plane
        # gates only the static consent step, not the reversible money/books steps.
        "billing": Operator("billing", [
            capability("billing.create_subscription", lambda i: {"subscription_id": "sub_1"},
                       provides=["subscription"], side_effecting=True, undo="billing.cancel_subscription",
                       permissions=["billing:write"]),
            capability("billing.cancel_subscription", lambda i: {"cancelled": True},
                       side_effecting=True, permissions=["billing:write"])]),
        "support": Operator("support", [
            capability("support.send_onboarding", lambda i: {"onboarding_sent": True},
                       provides=["onboarding_sent"], side_effecting=True, permissions=["support:write"])]),
        "books": Operator("books", [
            capability("books.record_revenue", lambda i: {"entry": "je_1"},
                       provides=["revenue_recorded"], side_effecting=True, undo="books.reverse_entry",
                       permissions=["books:write"]),
            capability("books.reverse_entry", lambda i: {"reversed": True},
                       side_effecting=True, permissions=["books:write"])]),
        "compliance": Operator("compliance", [
            capability("compliance.file_consent", lambda i: {"consent_id": "gdpr_1"},
                       provides=["consent_filed"], side_effecting=True, approval_required=True,
                       permissions=["compliance:write"])]),
    }


def _build_runtime(store: EventStore) -> MissionRuntime:
    ops = _fleet()
    reg = CapabilityRegistry()
    for op in ops.values():
        reg.register(op.manifest)
    return MissionRuntime(reg, Executor(LocalOperatorClient(ops)), store=store)


def test_ci_passes_for_a_good_mission():
    report = run_mission_ci(_build_runtime, goal="Onboard a new customer", template="onboarding",
                            grants=GRANTS, approve=["compliance.file_consent"], golden=GOLDEN)
    assert report.passed, report.failures()
    assert report.summary() == {"feasibility": True, "budget": True, "run": True,
                                "regression": True, "replay": True}


def test_ci_feasibility_fails_without_grants():
    report = run_mission_ci(_build_runtime, goal="Onboard", template="onboarding",
                            grants=["billing:write"], approve=["compliance.file_consent"], golden=GOLDEN)
    assert not report.passed
    assert report.checks[0].name == "feasibility" and report.checks[0].passed is False


def test_ci_run_fails_when_a_gate_is_not_approved():
    # don't auto-approve the consent gate → mission parks, never reaches 'succeeded'
    report = run_mission_ci(_build_runtime, goal="Onboard", template="onboarding",
                            grants=GRANTS, approve=[], golden=GOLDEN)
    assert not report.passed
    run = next(c for c in report.checks if c.name == "run")
    assert run.passed is False and "waiting_human" in run.detail


def test_ci_regression_catches_a_missing_outcome():
    bogus = {"state": "succeeded", "world": ["subscription", "refund_issued"]}  # refund never happens
    report = run_mission_ci(_build_runtime, goal="Onboard", template="onboarding",
                            grants=GRANTS, approve=["compliance.file_consent"], golden=bogus)
    assert not report.passed
    reg = next(c for c in report.checks if c.name == "regression")
    assert reg.passed is False and "refund_issued" in reg.detail
