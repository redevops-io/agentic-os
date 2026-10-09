"""End-to-end: Priority Engine -> Decision bridge -> MissionRuntimeLauncher -> real MissionRuntime.

This is the plan's Phase-1 exit gate — a minimal app exercised through the whole runtime-native chain:
opportunity -> select_action -> bridge_selection(launcher) -> create_mission -> run -> [approve] ->
SUCCEEDED, with the InterventionRecord linked to the real mission id.
"""
from __future__ import annotations

from agentic_os.agent_gateway.contracts import ApprovalPolicy, RiskTier
from agentic_os.app_kit import BridgeOutcome, bridge_selection
from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher, drive
from agentic_os.intervention_record import InMemoryInterventionStore
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient, Operator, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import MissionState
from agentic_os.priority_engine import (
    Action,
    DecisionOpportunity,
    InterventionCandidate,
    select_action,
)


def _runtime(operator: Operator) -> MissionRuntime:
    registry = CapabilityRegistry()
    registry.register(operator.manifest)
    client = LocalOperatorClient({operator.name: operator})
    return MissionRuntime(registry, Executor(client))


def _demo_operator() -> Operator:
    # two capabilities with distinct outcomes; one auto, one approval-gated
    return Operator("demo-crm", [
        capability("demo.draft_outreach", lambda i: {"draft_outreach": "Hi there"},
                   provides=["draft_outreach"], outputs={"draft_outreach": "string"}),
        capability("demo.send_proposal", lambda i: {"send_proposal": "sent"},
                   provides=["send_proposal"], outputs={"send_proposal": "string"},
                   side_effecting=True, approval_required=True, undo="demo.unsend"),
    ])


def _candidate(proposed_action, action_kind, cap, *, risk, approval):
    return InterventionCandidate(
        source_app="demo-crm", subject="ACME", proposed_action=proposed_action,
        expected_value=8.0, confidence=0.95, risk_tier=risk, approval_policy=approval,
        required_capabilities=(cap,), candidate_id=f"c-{action_kind}", action_kind=action_kind,
    )


def _select(candidate):
    opp = DecisionOpportunity(entity=candidate.subject, source_app=candidate.source_app,
                              candidate_actions=(candidate,), opportunity_id="opp-1")
    return select_action(opp)


def test_ungated_action_runs_to_succeeded_end_to_end():
    op = _demo_operator()
    rt = _runtime(op)
    launcher = MissionRuntimeLauncher(rt)
    store = InMemoryInterventionStore()

    cand = _candidate("draft outreach", "draft_outreach", "demo.draft_outreach",
                      risk=RiskTier.READ, approval=ApprovalPolicy.AUTO)
    sel = _select(cand)
    assert sel.decision.action is Action.ACT

    res = bridge_selection(sel, store=store, policy_version="assist-1", launcher=launcher)
    assert res.outcome is BridgeOutcome.LAUNCHED
    assert res.mission_id                                   # a real mission id was minted
    assert res.record.mission_id == res.mission_id          # the record links to it

    mission = drive(rt, res.mission_id)                     # run it
    assert mission.state is MissionState.SUCCEEDED
    assert "draft_outreach" in str(rt._world(res.mission_id).snapshot())  # capability output landed


def test_gated_action_parks_then_executes_on_approval():
    op = _demo_operator()
    rt = _runtime(op)
    launcher = MissionRuntimeLauncher(rt)
    store = InMemoryInterventionStore()

    cand = _candidate("send proposal", "send_proposal", "demo.send_proposal",
                      risk=RiskTier.CONSEQUENTIAL, approval=ApprovalPolicy.REQUIRED)
    sel = _select(cand)
    assert sel.decision.action is Action.REQUEST_APPROVAL

    res = bridge_selection(sel, store=store, policy_version="assist-1", launcher=launcher)
    assert res.outcome is BridgeOutcome.LAUNCHED_GATED

    # run without approving: it parks on the human gate (N3 — gated node does nothing yet)
    rt.run(res.mission_id)
    assert rt._missions[res.mission_id].state is MissionState.WAITING_HUMAN
    assert "send_proposal" not in str(rt._world(res.mission_id).snapshot())  # nothing executed yet

    # approve: the action actually executes on resume (N3)
    node_id = rt.repo.pending_human(res.mission_id)["node_id"]
    rt.approve(res.mission_id, node_id, "approve")
    assert rt._missions[res.mission_id].state is MissionState.SUCCEEDED
    assert "send_proposal" in str(rt._world(res.mission_id).snapshot())  # executed on approval


def test_drive_auto_approves_gate():
    op = _demo_operator()
    rt = _runtime(op)
    store = InMemoryInterventionStore()
    cand = _candidate("send proposal", "send_proposal", "demo.send_proposal",
                      risk=RiskTier.CONSEQUENTIAL, approval=ApprovalPolicy.REQUIRED)
    res = bridge_selection(_select(cand), store=store, policy_version="v",
                           launcher=MissionRuntimeLauncher(rt))
    mission = drive(rt, res.mission_id)                     # auto-approves the gate
    assert mission.state is MissionState.SUCCEEDED


def test_launch_carries_inputs_and_refuses_a_mis_bound_capability():
    """The launcher must (1) carry the decision's inputs onto the mission and (2) refuse to run a mission
    whose compiled plan bound a DIFFERENT capability than the decision selected — so an ambiguous goal can
    never resolve a gated `send_proposal` to an ungated `delete_all` and sidestep the gate."""
    import types
    import pytest
    from agentic_os.app_kit.decision_bridge import MissionRequest, BridgeError

    req = MissionRequest(goal="send the proposal", capability="demo.send_proposal",
                         required_capabilities=("demo.send_proposal",), inputs={"account": "A1"},
                         gated=True, intervention_id="iv-1", opportunity_id="op-1",
                         action_kind="send", subject="ACME")

    class _Stub:
        def __init__(self, bound):
            self._bound, self._plans, self.got_inputs = bound, {}, None
        def create_mission(self, goal, *, policy_refs=None, inputs=None, **kw):
            self.got_inputs = inputs
            self._plans["m-1"] = types.SimpleNamespace(
                graph=types.SimpleNamespace(nodes=[types.SimpleNamespace(capability=c) for c in self._bound]))
            return types.SimpleNamespace(id="m-1")

    ok = _Stub(["demo.send_proposal"])
    assert MissionRuntimeLauncher(ok).launch(req) == "m-1"
    assert ok.got_inputs == {"account": "A1"}                       # inputs are no longer dropped

    wrong = _Stub(["demo.delete_all"])                              # discovery bound an unrelated capability
    with pytest.raises(BridgeError):
        MissionRuntimeLauncher(wrong).launch(req)                   # refused, not run
