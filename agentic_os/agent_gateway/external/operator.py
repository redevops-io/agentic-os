"""Phase 4 — the External Agent Operator (plan §6, §11 Phase 4).

ReDevOps → external agent as a *governed execution node*. This is a real
:class:`agentic_os.mission.operator_sdk.Operator`: its capabilities are ``personal_agent.*`` (all
side-effecting + approval-required), so the Mission Runtime only ever invokes them AFTER Governance has
authorized the action. The handler then drives the provider through the :class:`TaskManager` and emits a
projects :class:`~agentic_os.projects.contracts.ActionReceipt`.

Two invariants live here:
  * **No provider task before authorization** — the operator is only reachable through the governed
    executor; there is no direct call path that submits to the provider without a Decision.
  * **The receipt reflects VERIFIED truth, not the provider's claim** — a claimed success that fails
    verification (Phase 6) yields a HELD receipt, never SUCCEEDED (plan §6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from agentic_os.mission.operator_sdk import Capability, Operator, capability

from .contracts import (
    AgentIdentity, AgentPermissionScope, AgentTaskRequest, ExternalAgentAdapter, TaskState,
    TERMINAL_STATES)
from .lifecycle import TaskManager
from .verification import VerificationStatus, verify_result

# The governed outbound capabilities and their (optional) compensating capability for the undo window.
_OUTBOUND_CAPS: Tuple[Tuple[str, Optional[str]], ...] = (
    ("personal_agent.browser_task", None),
    ("personal_agent.form_submit", None),
    ("personal_agent.reservation", "personal_agent.cancel_reservation"),
    ("personal_agent.checkout", "personal_agent.refund"),
    ("personal_agent.connected_app_action", None),
)

_MAX_DRIVE_STEPS = 12       # bounded poll loop for the deterministic/in-loop drive (fake + short tasks)


@dataclass
class ExternalAgentOperator:
    """Wraps an ExternalAgentAdapter as a governed Operator. One TaskManager is shared across invokes so
    lifecycle + idempotency persist. The provider identity is bound per-request from the inputs."""

    adapter: ExternalAgentAdapter
    name: str = "external-agent"
    manager: TaskManager = field(init=False)
    operator: Operator = field(init=False)

    def __post_init__(self) -> None:
        self.manager = TaskManager(self.adapter)
        caps = [self._build_capability(cap, undo) for cap, undo in _OUTBOUND_CAPS]
        self.operator = Operator(self.name, caps)

    def _build_capability(self, cap_name: str, undo: Optional[str]) -> Capability:
        def handler(inputs: Mapping[str, Any]) -> dict:
            return self._run(cap_name, dict(inputs))
        return capability(
            cap_name, handler, operator=self.name, side_effecting=True, approval_required=True,
            undo=undo, deterministic=False, estimated_value="high",
            data_classifications=["external_agent"], isolation_class="external-provider")

    # ── the governed handler ───────────────────────────────────────────────────────
    def _run(self, cap_name: str, inputs: Dict[str, Any]) -> dict:
        decision_id = str(inputs.pop("decision_id", ""))
        idempotency_key = str(inputs.pop("idempotency_key", ""))
        request = self._request(cap_name, inputs, idempotency_key)

        # Fail closed: the provider must VERIFIED/POLICY_SCOPED-support this capability.
        if not self.adapter.capabilities().supports(cap_name):
            return self._held(request, decision_id, reason="capability not verified for provider")

        mt = self.manager.start(request, decision_id=decision_id)
        # Drive the task to a terminal or a genuine WAITING_* state (bounded; async waits persist).
        for _ in range(_MAX_DRIVE_STEPS):
            if mt.state in TERMINAL_STATES or mt.state in (
                    TaskState.WAITING_FOR_INPUT, TaskState.WAITING_FOR_APPROVAL):
                break
            mt = self.manager.poll(mt.task_id)

        if mt.state in (TaskState.WAITING_FOR_INPUT, TaskState.WAITING_FOR_APPROVAL):
            return {"task_id": mt.task_id, "state": mt.state.value, "pending": True,
                    "receipt": self._receipt(request, decision_id, status="HELD",
                                             external_id=mt.provider_task_ref,
                                             error=f"awaiting {mt.state.value}").to_dict()}

        result = self.adapter.get_result(self.manager.ref(mt.task_id))
        ver = verify_result(request.intent_digest(), self.manager.ref(mt.task_id), result)

        if ver.is_success:
            status = "SUCCEEDED"
            error = ""
        elif ver.status is VerificationStatus.REFUTED:
            status, error = "HELD", f"verification refuted: {ver.reason}"      # never trust false success
        elif result.state is TaskState.FAILED:
            status, error = "FAILED", result.error or "provider failure"
        elif result.state is TaskState.CANCELLED:
            status, error = "FAILED", "task cancelled"
        else:
            status, error = "HELD", f"unverified ({ver.status.value}): {ver.reason}"

        receipt = self._receipt(request, decision_id, status=status,
                                external_id=result.provider_task_ref, error=error)
        return {"task_id": mt.task_id, "state": mt.state.value,
                "verification": ver.status.value, "verified": ver.is_success,
                "result": result.canonical(), "receipt": receipt.to_dict()}

    # ── helpers ──────────────────────────────────────────────────────────────────
    def _request(self, cap_name: str, inputs: Dict[str, Any], idem: str) -> AgentTaskRequest:
        ident = inputs.pop("identity", None) or {}
        identity = AgentIdentity(
            provider=ident.get("provider", getattr(self.adapter, "provider", "unknown")),
            adapter_version=ident.get("adapter_version", getattr(self.adapter, "adapter_version", "v1")),
            instance_id=ident.get("instance_id", "inst"))
        scope = inputs.pop("permission_scope", None)
        permission_scope = scope if isinstance(scope, AgentPermissionScope) else AgentPermissionScope()
        return AgentTaskRequest(
            identity=identity, capability=cap_name, goal=str(inputs.pop("goal", "")),
            inputs=inputs.pop("inputs", inputs), bounded_context=inputs.pop("bounded_context", {}),
            permission_scope=permission_scope, project_id=str(inputs.pop("project_id", "")),
            mission_id=str(inputs.pop("mission_id", "")), idempotency_key=idem)

    def _receipt(self, request: AgentTaskRequest, decision_id: str, *, status: str,
                 external_id: str = "", external_url: str = "", error: str = ""):
        # Lazy import: agentic_os.projects.__init__ pulls discovery_runtime (via workflow_learning), which
        # the core external path otherwise does not need. Import only when a receipt is actually produced.
        from agentic_os.projects.contracts import ActionReceipt  # noqa: PLC0415
        return ActionReceipt(
            artifact_id=request.request_id, capability=request.capability,
            provider=request.identity.provider, status=status, external_id=external_id,
            external_url=external_url, error=error, decision_id=decision_id)

    def _held(self, request: AgentTaskRequest, decision_id: str, *, reason: str) -> dict:
        return {"task_id": "", "state": "held", "verified": False,
                "receipt": self._receipt(request, decision_id, status="HELD", error=reason).to_dict()}
