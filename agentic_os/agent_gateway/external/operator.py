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

from agentic_os.app_gate import kernel_governed
from agentic_os.mission.operator_sdk import CURRENT_IDEMPOTENCY_KEY, Capability, Operator, capability

from .contracts import (
    AgentIdentity, AgentPermissionScope, AgentTaskRequest, ExternalAgentAdapter, TaskState,
    TERMINAL_STATES)
from .lifecycle import TaskManager
from .verification import PostconditionProbe, ProbeResult, VerificationStatus, verify_result

# The governed outbound capabilities and their (optional) compensating capability for the undo window.
_OUTBOUND_CAPS: Tuple[Tuple[str, Optional[str]], ...] = (
    ("personal_agent.browser_task", None),
    ("personal_agent.form_submit", None),
    ("personal_agent.reservation", "personal_agent.cancel_reservation"),
    ("personal_agent.checkout", "personal_agent.refund"),
    ("personal_agent.connected_app_action", None),
)

# errors after which an effect MAY already exist: never treated as a clean failure
_AMBIGUOUS = (TimeoutError, ConnectionError, OSError)

_MAX_DRIVE_STEPS = 12       # bounded poll loop for the deterministic/in-loop drive (fake + short tasks)


@dataclass
class ExternalAgentOperator:
    """Wraps an ExternalAgentAdapter as a governed Operator. One TaskManager is shared across invokes so
    lifecycle + idempotency persist. The provider identity is bound per-request from the inputs."""

    adapter: ExternalAgentAdapter
    name: str = "external-agent"
    # An INDEPENDENT read of the postcondition. Defaults to the adapter's own ``probe`` when it offers one.
    probe: Optional[PostconditionProbe] = None
    # Forbid minting VERIFIED from the provider's own artifact refs. Default: on for any non-simulated adapter.
    require_independent: Optional[bool] = None
    manager: TaskManager = field(init=False)
    operator: Operator = field(init=False)

    def __post_init__(self) -> None:
        self.manager = TaskManager(self.adapter)
        if self.probe is None and hasattr(self.adapter, "probe"):
            self.probe = self.adapter                                  # type: ignore[assignment]
        if self.require_independent is None:
            self.require_independent = not getattr(self.adapter, "simulated", False)
        caps = [self._build_capability(cap, undo) for cap, undo in _OUTBOUND_CAPS]
        self.operator = Operator(self.name, caps)
        kernel_governed(self.operator, "the Agent Gateway's external-agent operator: governed by the gateway's token auth, capability filtering and approvals")

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
        idempotency_key = str(inputs.pop("idempotency_key", "") or CURRENT_IDEMPOTENCY_KEY.get())
        request = self._request(cap_name, inputs, idempotency_key)

        # Fail closed: the provider must VERIFIED/POLICY_SCOPED-support this capability.
        if not self.adapter.capabilities().supports(cap_name):
            return self._held(request, decision_id, reason="capability not verified for provider")

        try:
            mt = self.manager.start(request, decision_id=decision_id)
            # Drive the task to a terminal or a genuine WAITING_* state (bounded; async waits persist).
            for _ in range(_MAX_DRIVE_STEPS):
                if mt.state in TERMINAL_STATES or mt.state in (
                        TaskState.WAITING_FOR_INPUT, TaskState.WAITING_FOR_APPROVAL):
                    break
                mt = self.manager.poll(mt.task_id)
        except _AMBIGUOUS as exc:
            # A timeout / dropped connection after a possible commit is NOT a failure: the effect may exist.
            return self._unknown(request, decision_id, idempotency_key,
                                 reason=f"outcome unconfirmed ({type(exc).__name__}) — reconcile before retrying")

        if mt.state in (TaskState.WAITING_FOR_INPUT, TaskState.WAITING_FOR_APPROVAL):
            return {"task_id": mt.task_id, "state": mt.state.value, "pending": True,
                    "receipt": self._receipt(request, decision_id, status="HELD",
                                             external_id=mt.provider_task_ref,
                                             error=f"awaiting {mt.state.value}").to_dict()}

        try:
            result = self.adapter.get_result(self.manager.ref(mt.task_id))
        except _AMBIGUOUS as exc:
            return self._unknown(request, decision_id, idempotency_key, task_id=mt.task_id,
                                 reason=f"outcome unconfirmed ({type(exc).__name__}) — reconcile before retrying")
        ver = verify_result(request.intent_digest(), self.manager.ref(mt.task_id), result,
                            probe=self.probe, require_independent=bool(self.require_independent))

        if ver.is_success:
            status = "SUCCEEDED"
            error = ""
        elif ver.status is VerificationStatus.REFUTED:
            status, error = "HELD", f"verification refuted: {ver.reason}"      # never trust false success
        elif ver.status is VerificationStatus.UNKNOWN:
            return self._unknown(request, decision_id, idempotency_key, task_id=mt.task_id,
                                 reason=f"unverified (unknown): {ver.reason}", result=result, ver=ver)
        elif result.state is TaskState.FAILED:
            status, error = "FAILED", result.error or "provider failure"
        elif result.state is TaskState.CANCELLED:
            status, error = "FAILED", "task cancelled"
        else:
            status, error = "HELD", f"unverified ({ver.status.value}): {ver.reason}"

        receipt = self._receipt(request, decision_id, status=status,
                                external_id=result.provider_task_ref, error=error)
        out = {"task_id": mt.task_id, "state": mt.state.value,
               "verification": ver.status.value, "verified": ver.is_success,
               "result": result.canonical(), "receipt": receipt.to_dict()}
        # a provider that CLAIMED success but could not be independently confirmed may still have acted:
        # the caller must reconcile before it retries (a fresh idempotency key could double-send)
        if status == "HELD" and result.state is TaskState.SUCCEEDED:
            out["outcome_unknown"] = True
        return out

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

    def _unknown(self, request: AgentTaskRequest, decision_id: str, idempotency_key: str, *, reason: str,
                 task_id: str = "", result=None, ver=None) -> dict:
        """The effect may or may not exist. Not FAILED (a retry could duplicate it), not SUCCEEDED."""
        receipt = self._receipt(request, decision_id, status="HELD", error=reason)
        return {"task_id": task_id, "state": "unknown", "verified": False, "verification": "unknown",
                "outcome_unknown": True, "idempotency_key": idempotency_key, "receipt": receipt.to_dict()}

    # ── reconcile: find out what actually happened, from a read, before anything is retried ──────────
    def reconcile(self, idempotency_key: str) -> dict:
        """Look the effect up by idempotency key and verify it independently. Returns
        ``{"outcome": "found"|"absent"|"unknown", "reason": ..., "verification": ...}``. ``absent`` is only
        returned when the provider can answer a lookup AND has no record; no answer is always ``unknown``."""
        ref = None
        task_id = self.manager._by_idem.get(idempotency_key) if idempotency_key else None
        if task_id:
            ref = self.manager.ref(task_id)
        else:
            finder = getattr(self.adapter, "find_task_by_idempotency_key", None)
            if finder is None:
                return {"outcome": "unknown", "reason": "the provider offers no lookup by idempotency key"}
            try:
                ref = finder(idempotency_key)
            except _AMBIGUOUS as exc:
                return {"outcome": "unknown", "reason": f"lookup failed ({type(exc).__name__})"}
            if ref is None:
                # "no record" proves nothing unless the provider's lookup is authoritative (read-your-writes
                # consistent). A lookup that may lag, time out or miss is "unknown", never "absent": retrying on
                # that basis could duplicate the effect.
                if getattr(self.adapter, "authoritative_lookup", False):
                    return {"outcome": "absent", "reason": "the provider's authoritative lookup has no record of "
                                                            "this idempotency key"}
                return {"outcome": "unknown",
                        "reason": "no record found, but this provider's lookup is not declared authoritative: "
                                  "absence is not proof; a person must confirm"}
        try:
            for _ in range(_MAX_DRIVE_STEPS):              # a found-but-unfinished task: read its status forward
                status = self.adapter.get_task(ref)
                if status.state in TERMINAL_STATES or status.state in (
                        TaskState.WAITING_FOR_INPUT, TaskState.WAITING_FOR_APPROVAL):
                    break
            result = self.adapter.get_result(ref)
        except _AMBIGUOUS as exc:
            return {"outcome": "unknown", "reason": f"result lookup failed ({type(exc).__name__})"}
        if result.state is not TaskState.SUCCEEDED:
            definitive = result.state in (TaskState.FAILED, TaskState.CANCELLED)
            return {"outcome": "absent" if definitive else "unknown",
                    "reason": f"provider task state is {result.state.value}", "verification": "n/a"}
        ver = verify_result("", ref, result, probe=self.probe, require_independent=bool(self.require_independent))
        if ver.status is VerificationStatus.VERIFIED:
            return {"outcome": "found", "reason": ver.reason, "verification": ver.status.value,
                    "external_id": result.provider_task_ref}
        if ver.status is VerificationStatus.REFUTED:
            return {"outcome": "absent", "reason": ver.reason, "verification": ver.status.value}
        return {"outcome": "unknown", "reason": ver.reason, "verification": ver.status.value}

    def _held(self, request: AgentTaskRequest, decision_id: str, *, reason: str) -> dict:
        return {"task_id": "", "state": "held", "verified": False,
                "receipt": self._receipt(request, decision_id, status="HELD", error=reason).to_dict()}
