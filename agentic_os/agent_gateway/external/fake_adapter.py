"""Phase 8 (fixture) — a deterministic in-repo ExternalAgentAdapter for conformance + adversarial tests.

This simulates the PROVIDER side (what a Muse-like agent tracks) with no external dependency, so the
whole gateway — governance, lifecycle, evidence, verification, Edge Sentinel observation — can be
proven end-to-end offline. It is the only VERIFIED provider in external_agent_capabilities.yaml
precisely because it is deterministic and in-repo.

The task behavior is scripted by ``request.inputs["_outcome"]`` so tests can exercise each path:

  "succeed"        PENDING → RUNNING → SUCCEEDED (with an artifact + evidence)
  "need_input"     … → WAITING_FOR_INPUT ; provide_input → RUNNING → SUCCEEDED
  "need_approval"  … → WAITING_FOR_APPROVAL → SUCCEEDED (a PROVIDER-side approval; never authoritative)
  "fail"           … → FAILED
  "false_success"  … → SUCCEEDED but provider_claimed_success=True with NO artifact/evidence
                       (the adversarial "provider lies about success" case — verification must catch it)

The provider advances one step per ``get_task`` poll (deterministic and restart-safe); it blocks at a
WAITING_* step until unblocked (provide_input, or an internal self-approve on the next poll).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .contracts import (
    AgentCapabilities, AgentTaskInput, AgentTaskRef, AgentTaskRequest, AgentTaskResult,
    AgentTaskStatus, CapabilityStatus, TaskState)

# The personal-agent capabilities the fake reports as VERIFIED (mirrors the operator surface).
_FAKE_CAPS = {
    "personal_agent.browser_task": CapabilityStatus.VERIFIED,
    "personal_agent.form_submit": CapabilityStatus.VERIFIED,
    "personal_agent.reservation": CapabilityStatus.VERIFIED,
    "personal_agent.checkout": CapabilityStatus.VERIFIED,
    "personal_agent.connected_app_action": CapabilityStatus.VERIFIED,
    "security_inspection": CapabilityStatus.VERIFIED,     # an inbound goal type
}


@dataclass
class _ProviderTask:
    provider_task_ref: str
    capability: str
    outcome: str
    intent_digest: str
    trajectory: List[TaskState]
    cursor: int = 0
    unblocked_input: bool = False

    @property
    def current(self) -> TaskState:
        return self.trajectory[min(self.cursor, len(self.trajectory) - 1)]


def _trajectory(outcome: str) -> List[TaskState]:
    if outcome == "need_input":
        return [TaskState.PENDING, TaskState.RUNNING, TaskState.WAITING_FOR_INPUT]
    if outcome == "need_approval":
        return [TaskState.PENDING, TaskState.RUNNING, TaskState.WAITING_FOR_APPROVAL, TaskState.SUCCEEDED]
    if outcome == "fail":
        return [TaskState.PENDING, TaskState.RUNNING, TaskState.FAILED]
    # "succeed" and "false_success" share the shape; they differ only in the result payload.
    return [TaskState.PENDING, TaskState.RUNNING, TaskState.SUCCEEDED]


@dataclass
class FakeExternalAgentAdapter:
    """A deterministic ExternalAgentAdapter. Structural-typed against the Protocol (no inheritance)."""

    provider: str = "fake-external-agent"
    adapter_version: str = "v1"
    _tasks: Dict[str, _ProviderTask] = field(default_factory=dict)
    _counter: int = 0

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(provider=self.provider, statuses=dict(_FAKE_CAPS))

    def submit_task(self, request: AgentTaskRequest) -> AgentTaskRef:
        outcome = str(request.inputs.get("_outcome", "succeed"))
        self._counter += 1
        ptref = f"{self.provider}:task:{self._counter}"
        self._tasks[ptref] = _ProviderTask(
            provider_task_ref=ptref, capability=request.capability, outcome=outcome,
            intent_digest=request.intent_digest(), trajectory=_trajectory(outcome))
        return AgentTaskRef.new(self.provider, provider_task_ref=ptref,
                                intent_digest=request.intent_digest())

    def get_task(self, ref: AgentTaskRef) -> AgentTaskStatus:
        t = self._require(ref)
        # Advance one step per poll unless blocked at a WAITING_FOR_INPUT the caller hasn't answered.
        if t.current is TaskState.WAITING_FOR_INPUT and not t.unblocked_input:
            pass                                   # stays parked until provide_input
        elif t.cursor < len(t.trajectory) - 1:
            t.cursor += 1
        return self._status(t)

    def provide_input(self, ref: AgentTaskRef, value: AgentTaskInput) -> AgentTaskStatus:
        t = self._require(ref)
        if t.current is TaskState.WAITING_FOR_INPUT and not t.unblocked_input:
            t.unblocked_input = True
            t.trajectory = t.trajectory + [TaskState.RUNNING, TaskState.SUCCEEDED]
            t.cursor += 1
        return self._status(t)

    def cancel_task(self, ref: AgentTaskRef) -> AgentTaskStatus:
        t = self._require(ref)
        if t.current not in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.EXPIRED):
            t.trajectory = t.trajectory[: t.cursor + 1] + [TaskState.CANCELLED]
            t.cursor += 1
        return self._status(t)

    def get_result(self, ref: AgentTaskRef) -> AgentTaskResult:
        t = self._require(ref)
        state = t.current
        if state is TaskState.SUCCEEDED and t.outcome != "false_success":
            return AgentTaskResult(
                task_id=ref.task_id, state=state, provider_claimed_success=True,
                normalized_outcome={"capability": t.capability, "ok": True},
                artifact_refs=(f"artifact:{t.provider_task_ref}",),
                evidence_refs=(f"evidence:{t.provider_task_ref}",),
                provider_task_ref=t.provider_task_ref)
        if state is TaskState.SUCCEEDED and t.outcome == "false_success":
            # Adversarial: the provider CLAIMS success but returns no artifact and no evidence.
            return AgentTaskResult(
                task_id=ref.task_id, state=state, provider_claimed_success=True,
                normalized_outcome={"capability": t.capability, "ok": True},
                artifact_refs=(), evidence_refs=(), provider_task_ref=t.provider_task_ref)
        if state is TaskState.FAILED:
            return AgentTaskResult(task_id=ref.task_id, state=state, provider_claimed_success=False,
                                   error="provider reported failure", provider_task_ref=t.provider_task_ref)
        return AgentTaskResult(task_id=ref.task_id, state=state, provider_claimed_success=False,
                               provider_task_ref=t.provider_task_ref)

    # ── internals ────────────────────────────────────────────────────────────────
    def _require(self, ref: AgentTaskRef) -> _ProviderTask:
        t = self._tasks.get(ref.provider_task_ref)
        if t is None:
            raise KeyError(f"no such provider task: {ref.provider_task_ref}")
        return t

    def _status(self, t: _ProviderTask) -> AgentTaskStatus:
        needs: dict = {}
        if t.current is TaskState.WAITING_FOR_INPUT:
            needs = {"input_id": f"{t.provider_task_ref}:q1", "prompt": "confirm details"}
        elif t.current is TaskState.WAITING_FOR_APPROVAL:
            needs = {"provider_approval_ref": f"{t.provider_task_ref}:appr"}
        return AgentTaskStatus(task_id=t.provider_task_ref, state=t.current,
                               detail=f"{t.capability}:{t.outcome}", needs=needs)
