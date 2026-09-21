"""Phase 7 — Edge Sentinel observation (plan §9, §11 Phase 7).

External-agent activity is another runtime-security observation source. This maps gateway/operator
events onto the REAL ``agentic_os.mission.events.RuntimeEvent`` (schema ``runtime-event/v10``) envelope
that Edge Sentinel's ``ai_security.py`` already ingests. It is strictly **observer-only**: it emits
evidence, it never authorizes and never gates. Anomalies (post-cancel execution, adapter-identity change,
repeated denials, stale/replayed approval, cross-Project access) surface as flagged events, not blocks —
Edge Sentinel investigates them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from agentic_os.mission.events import (
    ResultStatus, RuntimeEvent, capability_event, policy_decision_event, tool_result_event)

from .contracts import AgentTaskRef, AgentTaskRequest
from .lifecycle import ManagedTask

_SOURCE_RUNTIME = "external-agent-gateway"


def _actor(request: AgentTaskRequest) -> str:
    return f"{request.identity.provider}:{request.identity.instance_id}"


@dataclass
class ExternalAgentObserver:
    """Emits RuntimeEvents for external-agent activity and tracks light per-provider state so it can
    flag anomalies. Holds no authority; Edge Sentinel consumes what it emits."""

    emitted: List[RuntimeEvent] = field(default_factory=list)
    _denials: Dict[str, int] = field(default_factory=dict)          # provider -> consecutive denials
    _instance_of: Dict[str, str] = field(default_factory=dict)      # principal -> last instance_id seen

    def _emit(self, ev: RuntimeEvent) -> RuntimeEvent:
        self.emitted.append(ev)
        return ev

    def on_submit(self, request: AgentTaskRequest, ref: AgentTaskRef) -> RuntimeEvent:
        anomalies = self._identity_anomalies(request)
        return self._emit(capability_event(
            _actor(request), request.created_at, request.capability, result_status=ResultStatus.ATTEMPTED,
            source_runtime=_SOURCE_RUNTIME, source_agent=request.identity.provider,
            payload={"task_ref": ref.provider_task_ref, "intent_digest": ref.intent_digest,
                     "project_id": request.project_id, "mission_id": request.mission_id,
                     "anomalies": anomalies}))

    def on_result(self, request: AgentTaskRequest, *, receipt_status: str,
                  verification: str, task_id: str) -> RuntimeEvent:
        status = {"SUCCEEDED": ResultStatus.COMPLETED, "FAILED": ResultStatus.FAILED}.get(
            receipt_status, ResultStatus.OBSERVED)
        return self._emit(tool_result_event(
            _actor(request), request.created_at, request.capability, result_status=status,
            source_runtime=_SOURCE_RUNTIME, source_agent=request.identity.provider,
            payload={"task_id": task_id, "receipt_status": receipt_status, "verification": verification,
                     # a receipt that is not SUCCEEDED despite a provider success claim is an action/result
                     # mismatch worth surfacing — Edge Sentinel decides what to make of it.
                     "action_result_mismatch": receipt_status not in ("SUCCEEDED",) and verification == "refuted"}))

    def on_denied(self, request: AgentTaskRequest, reason: str) -> RuntimeEvent:
        self._denials[request.identity.provider] = self._denials.get(request.identity.provider, 0) + 1
        return self._emit(policy_decision_event(
            _actor(request), request.created_at, policy_context=f"external-agent:{request.capability}",
            result_status=ResultStatus.DENIED, source_runtime=_SOURCE_RUNTIME,
            source_agent=request.identity.provider,
            payload={"reason": reason, "capability": request.capability,
                     "consecutive_denials": self._denials[request.identity.provider]}))

    def on_allowed(self, request: AgentTaskRequest) -> None:
        self._denials[request.identity.provider] = 0        # reset the denial streak on an allow

    def on_post_cancel_execution(self, task: ManagedTask) -> RuntimeEvent:
        """A provider that keeps executing after we cancelled — a strong anomaly (plan §10)."""
        return self._emit(policy_decision_event(
            f"{task.provider}:{task.provider_task_ref}", int(time.time() * 1000),
            policy_context="external-agent:post-cancel",
            result_status=ResultStatus.DENIED, source_runtime=_SOURCE_RUNTIME, source_agent=task.provider,
            payload={"task_id": task.task_id, "anomaly": "execution_after_cancellation"}))

    def _identity_anomalies(self, request: AgentTaskRequest) -> list:
        anomalies: list = []
        principal = request.identity.principal.id if request.identity.principal else ""
        if principal:
            prev = self._instance_of.get(principal)
            if prev is not None and prev != request.identity.instance_id:
                anomalies.append("adapter_identity_change")
            self._instance_of[principal] = request.identity.instance_id
        return anomalies
