"""Phase 5 — background task lifecycle (plan §11 Phase 5).

The provider (adapter) tracks its own task; this is ReDevOps's DURABLE view of it. Responsibilities the
adapter must not be trusted to enforce:

  * **Idempotent start** — the same ``idempotency_key`` never starts two provider tasks.
  * **Monotonic, cancellation-aware state** — once we record a terminal state it never changes; once we
    CANCEL, no later provider poll can flip us to SUCCEEDED (plan §10 "execution after cancellation").
  * **Duplicate-callback safety** — a repeated provider callback/poll cannot duplicate a side effect.
  * **Restart safety** — the store round-trips to plain dicts so a manager can be rehydrated.

The manager holds a reference to the authorized intent digest and (once governed) the Decision id, so
a result can always be re-bound to the exact request that authorized it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .contracts import (
    AgentTaskInput, AgentTaskRef, AgentTaskRequest, AgentTaskStatus, ExternalAgentAdapter,
    TaskState, TERMINAL_STATES)


@dataclass
class ManagedTask:
    """ReDevOps's durable record of one external-agent task."""
    task_id: str
    provider: str
    provider_task_ref: str
    intent_digest: str
    capability: str
    state: TaskState = TaskState.PENDING
    idempotency_key: str = ""
    mission_id: str = ""
    decision_id: str = ""                       # set once the outbound action is governed (Phase 4)
    project_id: str = ""
    cancelled_locally: bool = False
    seen_callbacks: List[str] = field(default_factory=list)   # dedupe of provider callback ids
    detail: str = ""

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["state"] = self.state.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "ManagedTask":
        d = dict(d)
        d["state"] = TaskState(d["state"])
        return ManagedTask(**d)


class TaskManagerError(RuntimeError):
    pass


@dataclass
class TaskManager:
    """Owns the ReDevOps-side lifecycle for one adapter. In-memory but restart-safe via ``snapshot`` /
    ``restore``. Not thread-safe by itself (wrap in the control plane's task loop)."""

    adapter: ExternalAgentAdapter
    _tasks: Dict[str, ManagedTask] = field(default_factory=dict)          # task_id -> ManagedTask
    _by_idem: Dict[str, str] = field(default_factory=dict)               # idempotency_key -> task_id
    _refs: Dict[str, AgentTaskRef] = field(default_factory=dict)         # task_id -> provider ref

    # ── start / dedupe ───────────────────────────────────────────────────────────
    def start(self, request: AgentTaskRequest, *, decision_id: str = "") -> ManagedTask:
        key = request.idempotency_key
        if key and key in self._by_idem:
            return self._tasks[self._by_idem[key]]            # idempotent replay — no second submit
        ref = self.adapter.submit_task(request)
        mt = ManagedTask(
            task_id=ref.task_id, provider=ref.provider, provider_task_ref=ref.provider_task_ref,
            intent_digest=request.intent_digest(), capability=request.capability,
            idempotency_key=key, mission_id=request.mission_id, project_id=request.project_id,
            decision_id=decision_id)
        self._tasks[mt.task_id] = mt
        self._refs[mt.task_id] = ref
        if key:
            self._by_idem[key] = mt.task_id
        return mt

    # ── poll / advance (monotonic + cancellation-aware) ───────────────────────────
    def poll(self, task_id: str, *, callback_id: str = "") -> ManagedTask:
        mt = self._require(task_id)
        if callback_id:
            if callback_id in mt.seen_callbacks:
                return mt                                     # duplicate callback → no-op (idempotent)
            mt.seen_callbacks.append(callback_id)
        if mt.state in TERMINAL_STATES:
            return mt                                         # terminal is immutable
        status = self.adapter.get_task(self._refs[task_id])
        return self._apply(mt, status)

    def provide_input(self, task_id: str, value: AgentTaskInput) -> ManagedTask:
        mt = self._require(task_id)
        if mt.state in TERMINAL_STATES:
            raise TaskManagerError(f"task {task_id} is terminal ({mt.state.value}); cannot provide input")
        status = self.adapter.provide_input(self._refs[task_id], value)
        return self._apply(mt, status)

    def cancel(self, task_id: str, *, by: str = "") -> ManagedTask:
        mt = self._require(task_id)
        if mt.state in TERMINAL_STATES:
            return mt
        mt.cancelled_locally = True
        status = self.adapter.cancel_task(self._refs[task_id])
        # Regardless of what the provider returns, our authoritative state is CANCELLED.
        mt.state = TaskState.CANCELLED
        mt.detail = status.detail
        return mt

    def _apply(self, mt: ManagedTask, status: AgentTaskStatus) -> ManagedTask:
        # Cancellation is final on our side: a provider that keeps running and later "succeeds" cannot
        # move us out of CANCELLED (plan §10 post-cancel execution).
        if mt.cancelled_locally:
            mt.state = TaskState.CANCELLED
            return mt
        mt.state = status.state
        mt.detail = status.detail
        return mt

    # ── reads / persistence ───────────────────────────────────────────────────────
    def get(self, task_id: str) -> ManagedTask:
        return self._require(task_id)

    def ref(self, task_id: str) -> AgentTaskRef:
        return self._refs[self._require(task_id).task_id]

    def all(self) -> List[dict]:
        return [t.to_dict() for t in self._tasks.values()]

    def snapshot(self) -> dict:
        return {"tasks": {tid: t.to_dict() for tid, t in self._tasks.items()},
                "by_idem": dict(self._by_idem),
                "refs": {tid: {"task_id": r.task_id, "provider": r.provider,
                               "provider_task_ref": r.provider_task_ref,
                               "intent_digest": r.intent_digest} for tid, r in self._refs.items()}}

    def restore(self, snap: dict) -> "TaskManager":
        self._tasks = {tid: ManagedTask.from_dict(d) for tid, d in snap.get("tasks", {}).items()}
        self._by_idem = dict(snap.get("by_idem", {}))
        self._refs = {tid: AgentTaskRef(**r) for tid, r in snap.get("refs", {}).items()}
        return self

    def _require(self, task_id: str) -> ManagedTask:
        mt = self._tasks.get(task_id)
        if mt is None:
            raise TaskManagerError(f"no such task: {task_id}")
        return mt
