"""External Agent Gateway — cross-boundary E2E (integration truth) + the negative twin.

Proves the full realized chain composes end to end:

    external goal -> inbound Mission -> a finding (evidence) -> trusted approval bound to the exact
    intent digest (Decision) -> ExternalAgentOperator executes under that Decision -> ActionReceipt
    (decision_id stamped) -> verification (provider claim separated from observed truth) -> RuntimeEvent
    v10 emitted for Edge Sentinel observation.

And the negative twin: mutate or replay the authorization and prove ZERO external execution — no operator
invocation, no ActionReceipt claiming success, no provider task submitted.

Run:  PYTHONPATH=/mnt/backup/projects/discovery-runtime .venv/bin/python -m pytest \
        tests/test_external_agent_gateway_e2e.py -q      (or `uv run pytest` in the aligned env)
"""
from __future__ import annotations

import pytest

from agentic_os.overlays import Principal
from agentic_os.mission.events import SCHEMA_VERSION
from agentic_os.agent_gateway.external import (
    AgentIdentity, AgentPermissionScope, AgentTaskRequest, ExternalAgentObserver,
    ExternalAgentOperator, FakeExternalAgentAdapter)
from agentic_os.agent_gateway.external.approval_bridge import ApprovalError, ExternalApprovalBridge
from agentic_os.agent_gateway.external.inbound import InboundExternalAgentBridge


# ── a faithful MissionRuntime double (public contract only) ──────────────────────────
class _St:
    def __init__(self, name): self.name = name


class _Mission:
    def __init__(self, mid, state): self.id = mid; self.state = _St(state)


class _FakeRuntime:
    def __init__(self, run_state="WAITING_HUMAN"):
        self.run_state = run_state; self.created = []; self.ran = []
    def create_mission(self, goal, *, constraints=None):
        self.created.append((goal, tuple(constraints or ()))); return _Mission("mission:1", "PLANNED")
    def run(self, mid):
        self.ran.append(mid); return _Mission(mid, self.run_state)
    def missions(self):
        return [{"id": "mission:1", "state": self.run_state, "goal": "g"}]
    def inbox(self):
        return [{"mission_id": "mission:1", "node": "remediate"}]
    def explain(self, mid):
        return {"id": mid, "plan": ["inspect", "triage", "approve", "remediate"]}


def _principal():
    return Principal(id="user:alex", kind="user", roles=(), tenant="acme")


def _identity():
    return AgentIdentity(provider="fake-external-agent", adapter_version="v1", instance_id="inst-1",
                         principal=_principal())


def _action_request(*, finding_evidence="soev:finding-1", outcome="succeed", detail="x"):
    """The governed OUTBOUND action a finding warrants — carries the finding evidence ref + bounded ctx."""
    return AgentTaskRequest(
        identity=_identity(), capability="personal_agent.browser_task",
        goal="remediate the low-risk finding", inputs={"_outcome": outcome, "detail": detail},
        bounded_context={"finding_evidence": finding_evidence}, project_id="proj:acme",
        mission_id="mission:1", idempotency_key="idem-remediate-1")


def _governed_execute(bridge, operator, observer, request):
    """The governed caller: authorize FIRST (raises if not authorized), and ONLY THEN invoke the
    operator. This is the gate — the operator is never reached without a valid Decision."""
    decision = bridge.authorize(request)                      # raises ApprovalError if unauthorized
    out = operator.operator.invoke(
        request.capability,
        {"_outcome": request.inputs.get("_outcome", "succeed"), "detail": request.inputs.get("detail"),
         "decision_id": decision.decision_id, "mission_id": request.mission_id},
        request.idempotency_key)
    ev = observer.on_result(request, receipt_status=out["receipt"]["status"],
                            verification=out.get("verification", ""), task_id=out.get("task_id", ""))
    return out, decision, ev


# ── the happy-path E2E ────────────────────────────────────────────────────────────────
def test_e2e_goal_to_governed_action_receipt_verification_observation():
    # 1. external goal -> real inbound Mission
    rt = _FakeRuntime(run_state="WAITING_HUMAN")
    inbound = InboundExternalAgentBridge(rt)
    goal_req = AgentTaskRequest(
        identity=_identity(), capability="security_inspection",
        goal="inspect infra; fix low-risk automatically, ask before blocking traffic",
        permission_scope=AgentPermissionScope(ask_before_capabilities=("sentinel.block_ip",)),
        project_id="proj:acme")
    handle = inbound.submit_goal(goal_req)
    assert handle.mission_id == "mission:1" and handle.needs_approval is True

    # 2. a finding warrants a governed outbound remediation
    action = _action_request()

    # 3. trusted approval -> Decision bound to the EXACT intent digest
    adapter = FakeExternalAgentAdapter()
    operator = ExternalAgentOperator(adapter)
    bridge = ExternalApprovalBridge()
    observer = ExternalAgentObserver()
    apr = bridge.present(action)
    decision = bridge.approve(apr, principal=_principal(), by="alex", attestation="webauthn")

    # 4-5. governed execution -> ActionReceipt (decision_id stamped) -> verification -> RuntimeEvent
    out, authz, ev = _governed_execute(bridge, operator, observer, action)
    assert authz.decision_id == decision.decision_id
    assert out["receipt"]["status"] == "SUCCEEDED"
    assert out["receipt"]["decision_id"] == decision.decision_id      # the ONE authorization object
    assert out["verified"] is True                                    # verified, not just provider-claimed
    ev.validate()
    assert ev.schema_version == SCHEMA_VERSION == "runtime-event/v10"
    assert len(adapter._tasks) == 1                                   # exactly one external execution


# ── the negative twin: mutate / replay -> ZERO external execution ────────────────────
def test_e2e_replayed_approval_yields_zero_execution():
    adapter = FakeExternalAgentAdapter()
    operator = ExternalAgentOperator(adapter)
    bridge = ExternalApprovalBridge()
    observer = ExternalAgentObserver()
    action = _action_request()
    apr = bridge.present(action)
    bridge.approve(apr, principal=_principal(), by="alex", attestation="webauthn")

    _governed_execute(bridge, operator, observer, action)            # first, authorized execution
    assert len(adapter._tasks) == 1

    # Replaying the SAME approved request must fail closed -> operator never reached -> no new task.
    with pytest.raises(ApprovalError):
        _governed_execute(bridge, operator, observer, action)
    assert len(adapter._tasks) == 1                                  # still exactly one; zero replay execution


def test_e2e_mutated_action_after_approval_yields_zero_execution():
    adapter = FakeExternalAgentAdapter()
    operator = ExternalAgentOperator(adapter)
    bridge = ExternalApprovalBridge()
    observer = ExternalAgentObserver()

    approved = _action_request(detail="original")
    apr = bridge.present(approved)
    bridge.approve(apr, principal=_principal(), by="alex", attestation="webauthn")

    # A mutated action (different detail -> different intent digest) has no approval -> fail closed.
    mutated = _action_request(detail="tampered")
    assert mutated.intent_digest() != approved.intent_digest()
    with pytest.raises(ApprovalError):
        _governed_execute(bridge, operator, observer, mutated)
    assert len(adapter._tasks) == 0                                  # zero external execution for the mutation


def test_e2e_unapproved_action_never_reaches_the_operator():
    adapter = FakeExternalAgentAdapter()
    operator = ExternalAgentOperator(adapter)
    bridge = ExternalApprovalBridge()
    observer = ExternalAgentObserver()
    action = _action_request()
    bridge.present(action)                                           # presented but NEVER approved
    with pytest.raises(ApprovalError):
        _governed_execute(bridge, operator, observer, action)
    assert len(adapter._tasks) == 0
