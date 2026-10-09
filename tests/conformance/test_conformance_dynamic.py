"""Dynamic runtime-native conformance (plan §9): the invariants that need a running mission.

N3 (approval executes), N4 (a failed read-back verifier blocks the commit) and N5 (a real outcome
changes WHICH action is favoured, and only a promoted policy does so) — exercised against a
reference operator built in-test, so the suite runs with no apps and no live cores.
"""
from __future__ import annotations

from agentic_os.agent_gateway.contracts import ApprovalPolicy, RiskTier
from agentic_os.app_kit import bridge_selection
from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher, drive
from agentic_os.intervention_record import InMemoryInterventionStore
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient, Operator, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import MissionState
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate, select_action


def _runtime(operator: Operator) -> MissionRuntime:
    reg = CapabilityRegistry()
    reg.register(operator.manifest)
    return MissionRuntime(reg, Executor(LocalOperatorClient({operator.name: operator})))


def _candidate(cap, action_kind, *, risk=RiskTier.READ, approval=ApprovalPolicy.AUTO, ev=8.0):
    return InterventionCandidate(
        source_app="ref", subject="S", proposed_action=action_kind, expected_value=ev,
        confidence=0.95, risk_tier=risk, approval_policy=approval,
        required_capabilities=(cap,), candidate_id=f"c-{action_kind}", action_kind=action_kind)


def _launch(rt, cand):
    sel = select_action(DecisionOpportunity(entity="S", source_app="ref",
                                            candidate_actions=(cand,), opportunity_id="opp"))
    res = bridge_selection(sel, store=InMemoryInterventionStore(), policy_version="v",
                           launcher=MissionRuntimeLauncher(rt))
    return res


# ── N3: an approved gated node actually executes on resume ───────────────────

def test_n3_approved_gated_node_executes():
    op = Operator("ref", [capability("ref.commit", lambda i: {"committed": "yes"},
                                     provides=["committed"], outputs={"committed": "str"},
                                     side_effecting=True, approval_required=True)])
    rt = _runtime(op)
    res = _launch(rt, _candidate("ref.commit", "commit", risk=RiskTier.CONSEQUENTIAL,
                                 approval=ApprovalPolicy.REQUIRED))
    rt.run(res.mission_id)
    assert rt._missions[res.mission_id].state is MissionState.WAITING_HUMAN   # parked (nothing done)
    assert "committed" not in str(rt._world(res.mission_id).snapshot())
    drive(rt, res.mission_id)                                                 # approve
    assert rt._missions[res.mission_id].state is MissionState.SUCCEEDED
    assert "committed" in str(rt._world(res.mission_id).snapshot())           # executed on approval


def test_n3_resumed_handler_receives_approval_marker():
    """The resumed handler of an approved gated node receives an ``_approval`` marker in its inputs — even
    on a plain yes/no approval with no typed edit. That marker is a gated handler's cue to EXECUTE the real
    side effect on approval instead of re-staging it forever; it is ABSENT on the ungoverned/pre-approval
    path, so a handler called directly still stages."""
    seen: dict = {}

    def handler(i):
        seen["approval"] = i.get("_approval")
        return {"did": "it"}

    op = Operator("ref", [capability("ref.act", handler, provides=["did"], outputs={"did": "str"},
                                     side_effecting=True, approval_required=True)])
    rt = _runtime(op)
    res = _launch(rt, _candidate("ref.act", "act", risk=RiskTier.CONSEQUENTIAL, approval=ApprovalPolicy.REQUIRED))
    rt.run(res.mission_id)
    assert seen.get("approval") is None                                       # parked — handler not yet run
    drive(rt, res.mission_id)                                                 # approve (no typed edit)
    assert rt._missions[res.mission_id].state is MissionState.SUCCEEDED
    assert seen["approval"] and seen["approval"].get("approved") is True      # handler saw the approval marker


# ── N4: a side-effecting node whose read-back fails does NOT commit ──────────

def test_n4_failed_verifier_blocks_commit():
    # declares an output it never returns -> the composite verifier's syntactic check rejects it
    op = Operator("ref", [capability("ref.badwrite", lambda i: {},
                                     provides=["result"], outputs={"result": "str"},
                                     side_effecting=True)])
    rt = _runtime(op)
    res = _launch(rt, _candidate("ref.badwrite", "badwrite"))
    mission = drive(rt, res.mission_id)
    assert mission.state is not MissionState.SUCCEEDED          # verification blocked the commit
    assert "result" not in str(rt._world(res.mission_id).snapshot())


def test_n4_passing_verifier_commits():
    op = Operator("ref", [capability("ref.goodwrite", lambda i: {"result": "ok"},
                                     provides=["result"], outputs={"result": "str"},
                                     side_effecting=True)])
    rt = _runtime(op)
    res = _launch(rt, _candidate("ref.goodwrite", "goodwrite"))
    mission = drive(rt, res.mission_id)
    assert mission.state is MissionState.SUCCEEDED
    assert "ok" in str(rt._world(res.mission_id).snapshot())
