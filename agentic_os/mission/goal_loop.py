"""The goal-loop controller: pursue a goal across iterations, with ceilings that stop it and thresholds that make it think.

A mission runs a plan. A goal is bigger: a plan can succeed and leave the goal unmet, or fail in a way a different plan would not. This
controller owns that outer loop. It adds no new way to execute anything: each iteration is an ordinary mission, so every gate, authority
check, reservation and reconciliation rule still applies to it unchanged.

Two kinds of limit, treated differently on purpose:

  HARD limits (iterations, active time, usd, tokens, deadline) are ceilings. Reaching one STOPS the loop (`limit`), with the reason on
  the record. Only the owner, naming themselves and why, can raise them (`extend_limits`) and resume. Nothing is retried around a ceiling.

  SOFT thresholds (repeating the same failure, no new progress, the same plan again) are signals that the current approach is not
  working. They never terminate: the loop DIAGNOSES (records what is repeating, with evidence), asks the driver to REPLAN (a bounded number
  of times), and then ESCALATES to a person (`escalated`, resumable) instead of grinding on or giving up.

Invariants this module exists to hold:

  * A goal is complete only when an independent verifier says so with evidence. A succeeded mission is not a verified goal; without a
    verifier the loop ends `completed_unverified`, never `completed`.
  * Replanning is never a way around uncertainty or authority. While any iteration's mission waits on a reconciliation, no new iteration
    starts and nothing is replanned (a new plan could repeat the unknown effect). An authority denial, a mission-level ceiling, an unknown
    effect or a failed compensation escalates immediately: they are decisions for a person, not obstacles to plan around.
  * One driver per loop and per mission, enforced at the WRITE (fenced leases from `ownership.py`): a stale controller cannot append.
  * The loop's state is a pure fold of its own durable log. A restart resumes the open iteration (it never starts a second one for the same
    number) and never reruns finished work; counters cannot be reset by a crash.
  * Cancellation is recorded first, then propagated to the live mission. A call already in flight cannot be killed; what it leaves
    (including unresolved reconciliations) is listed on the cancellation record, never hidden.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Optional, Protocol

from .ownership import FencedEventStore, MissionLeases
from .reconcile import ReconciliationError, validate_evidence
from .types import MissionState, NodeState

_TERMINAL_MISSION = (MissionState.SUCCEEDED, MissionState.FAILED, MissionState.CANCELLED, MissionState.COMPENSATION_FAILED)
_DONE = ("completed", "completed_unverified", "cancelled")


class LoopError(Exception):
    pass


class LoopBusy(LoopError):
    """Another controller holds this loop (or its mission). Nothing was written or spent."""


# ───────────────────────────── configuration ─────────────────────────────
@dataclass(frozen=True)
class HardLimits:
    max_iterations: int = 10
    max_active_s: Optional[float] = None
    max_usd: Optional[float] = None
    max_tokens: Optional[int] = None
    deadline: Optional[float] = None               # absolute epoch seconds

    def to_dict(self) -> dict:
        return {"max_iterations": self.max_iterations, "max_active_s": self.max_active_s, "max_usd": self.max_usd,
                "max_tokens": self.max_tokens, "deadline": self.deadline}


@dataclass(frozen=True)
class SoftThresholds:
    repeat_failures: int = 2                       # the same failure signature this many times since the last replan
    stall_iterations: int = 3                      # this many iterations in a row with no new completed step
    plan_repeats: int = 2                          # the same plan signature this many times with no new progress
    max_replans: int = 2                           # bounded: after this the loop escalates instead of replanning again
    warn_fraction: float = 0.8                     # BUDGET_NEAR_LIMIT at this fraction of a hard cap

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class LoopSpec:
    goal: str
    tenant: str = ""
    limits: HardLimits = HardLimits()
    soft: SoftThresholds = SoftThresholds()
    owner: str = ""


# ───────────────────────────── driver / verifier contracts ─────────────────────────────
@dataclass(frozen=True)
class IterationContext:
    loop_id: str
    n: int                                          # 1-based iteration number
    goal: str
    guidance: tuple = ()                            # what replanning decided so far (oldest first)
    completed: frozenset = frozenset()              # step signatures already completed in earlier iterations: do not redo them
    last_failure: str = ""
    diagnosis: Optional[dict] = None                # set when this is a replan call

    @property
    def key(self) -> str:
        return f"{self.loop_id}:i{self.n}"


class LoopDriver(Protocol):
    def start(self, ctx: IterationContext) -> str:
        """Create the mission for this iteration (do not run it) and return its id."""

    def replan(self, ctx: IterationContext) -> Optional[dict]:
        """Return guidance for the next iteration (recorded verbatim), or None when there is no different approach to try."""


@dataclass(frozen=True)
class Verification:
    verified: bool
    evidence: Optional[dict] = None                 # {kind, reference, ...}: what was checked and where
    reason: str = ""


Verifier = Callable[[IterationContext, Any], Verification]


# ───────────────────────────── fold ─────────────────────────────
@dataclass
class Iteration:
    n: int
    mission_id: str
    started_at: float = 0.0
    finished: bool = False
    outcome: str = ""                               # succeeded | failed | hard_limit | authority_denied | needs_attention | unverified | cancelled
    progress: int = 0                               # NEW completed step signatures this iteration contributed
    usd: float = 0.0
    tokens: int = 0
    active_s: float = 0.0
    failure_sig: str = ""
    plan_sig: str = ""


@dataclass
class LoopState:
    loop_id: str
    started: bool = False
    spec: dict = field(default_factory=dict)
    status: str = "running"      # running|waiting_human|waiting_reconciliation|escalated|limit|completed|completed_unverified|cancelled
    iterations: list = field(default_factory=list)
    progress_seen: set = field(default_factory=set)
    guidance: list = field(default_factory=list)
    replans: int = 0
    replan_allowance: int = 0                       # replans in the current escalation epoch
    window_from: int = 0                            # index into `iterations` where the current strategy window starts
    warned: set = field(default_factory=set)
    cancel_requested: Optional[dict] = None
    escalation: Optional[dict] = None
    limit_block: Optional[dict] = None
    limits: dict = field(default_factory=dict)
    verified: Optional[dict] = None
    closed: Optional[dict] = None
    last_ts: float = 0.0
    last_diagnosis: Optional[dict] = None

    @property
    def terminal(self) -> bool:
        return self.status in _DONE

    @property
    def open_iteration(self) -> Optional[Iteration]:
        return self.iterations[-1] if self.iterations and not self.iterations[-1].finished else None

    def window(self) -> list:
        return [i for i in self.iterations[self.window_from:] if i.finished]

    def to_dict(self) -> dict:
        return {"loop_id": self.loop_id, "status": self.status, "iterations": [i.__dict__ for i in self.iterations],
                "replans": self.replans, "progress": len(self.progress_seen), "escalation": self.escalation,
                "limit_block": self.limit_block, "verified": self.verified, "closed": self.closed, "limits": self.limits}


def fold_loop(events: Iterable, loop_id: str = "") -> LoopState:
    """Pure fold of a loop's events. No clock, no randomness: same log, same state."""
    st = LoopState(loop_id=loop_id)
    for ev in events:
        t, p, ts = (tuple(ev) + (0.0,))[:3] if isinstance(ev, tuple) else (ev.type, ev.payload, getattr(ev, "ts", 0.0))
        p = p or {}
        st.last_ts = ts or st.last_ts
        if t == "LoopStarted":
            st.started, st.spec, st.limits = True, dict(p.get("spec") or {}), dict((p.get("spec") or {}).get("limits") or {})
            st.loop_id = st.loop_id or p.get("loop_id", "")
            st.replan_allowance = int(((p.get("spec") or {}).get("soft") or {}).get("max_replans", 2))
        elif not st.started or st.terminal:
            continue
        elif t == "IterationStarted":
            st.iterations.append(Iteration(n=int(p["n"]), mission_id=p["mission_id"], started_at=ts))
            st.status = "running"
        elif t == "IterationFinished":
            it = next((i for i in st.iterations if i.n == int(p["n"])), None)
            if it is not None and not it.finished:
                it.finished, it.outcome = True, p.get("outcome", "")
                it.usd, it.tokens = float(p.get("usd", 0.0)), int(p.get("tokens", 0))
                it.failure_sig, it.plan_sig = p.get("failure_sig", ""), p.get("plan_sig", "")
                new = [s for s in p.get("done_sigs", []) if s not in st.progress_seen]
                it.progress = len(new)
                st.progress_seen.update(new)
        elif t == "SegmentFinished":
            it = next((i for i in st.iterations if i.n == int(p["n"])), None)
            if it is not None:
                it.active_s += float(p.get("s", 0.0))
        elif t == "Diagnosed":
            st.last_diagnosis = dict(p)
        elif t == "Replanned":
            st.last_diagnosis = None
            st.replans += 1
            st.replan_allowance -= 1
            st.guidance.append(p.get("guidance"))
            st.window_from = len(st.iterations)
        elif t == "BudgetNearLimit":
            st.warned.add(p.get("cap"))
        elif t == "LoopEscalated":
            st.status, st.escalation = "escalated", dict(p)
        elif t == "EscalationResolved":
            st.escalation, st.last_diagnosis = None, None
            st.status = "running"
            if p.get("decision") == "continue":
                st.window_from = len(st.iterations)
                st.replan_allowance = int(p.get("replans", 1))
                if p.get("guidance") is not None:
                    st.guidance.append(p["guidance"])
        elif t == "LoopLimitReached":
            st.status, st.limit_block = "limit", dict(p)
        elif t == "LimitsExtended":
            st.limits = {**st.limits, **{k: v for k, v in (p.get("limits") or {}).items()}}
            st.limit_block = None
            st.warned.clear()
            st.status = "running"
        elif t == "LoopWaiting":
            st.status = p.get("status", "running")
        elif t == "LoopCancelRequested":
            st.cancel_requested = dict(p)
        elif t == "GoalVerified":
            st.verified = dict(p)
        elif t == "LoopCompleted":
            st.status, st.closed = ("completed" if p.get("verified") else "completed_unverified"), dict(p)
        elif t == "LoopCancelled":
            st.status, st.closed = "cancelled", dict(p)
    return st


# ───────────────────────────── helpers ─────────────────────────────
def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()[:16]


def _step_sigs(rt, mission_id: str) -> list:
    """Signatures of the steps this mission completed: what it did, not which random node id it carried."""
    plan = rt._plans.get(mission_id)
    if plan is None:
        return []
    status = rt.repo.node_status(mission_id)
    return sorted({f"{n.capability}|{n.produces or ''}|{_sha(n.inputs or {})}" for n in plan.graph.nodes if status.get(n.id) == NodeState.DONE})


def _plan_sig(rt, mission_id: str) -> str:
    plan = rt._plans.get(mission_id)
    if plan is None:
        return ""
    return _sha(sorted((n.capability, n.produces or "", _sha(n.inputs or {})) for n in plan.graph.nodes))


def _cost(rt, mission_id: str) -> tuple:
    plan = rt._plans.get(mission_id)
    if plan is None:
        return 0.0, 0
    status = rt.repo.node_status(mission_id)
    done = [n for n in plan.graph.nodes if status.get(n.id) == NodeState.DONE]
    return (sum(float(getattr(n.cost, "usd", 0.0) or 0.0) for n in done), sum(int(getattr(n.cost, "tokens", 0) or 0) for n in done))


# ───────────────────────────── the controller ─────────────────────────────
class GoalLoop:
    """`GoalLoop(runtime, driver, store=..., verifier=..., leases=...)`. `store` is the loop's own durable log (separate from the mission
    log, so loops never appear as missions). With `leases`, one driver per loop and per mission is enforced at the write; in the production
    profile leases are required and the mission runtime's store must be a `FencedEventStore`."""

    def __init__(self, runtime, driver: LoopDriver, *, store, verifier: Optional[Verifier] = None,
                 leases: Optional[MissionLeases] = None, on_event: Optional[Callable[[dict], None]] = None,
                 clock: Callable[[], float] = time.time, owner: str = "") -> None:
        from .profile import is_production  # noqa: PLC0415
        if is_production():
            if leases is None:
                raise LoopError("the production profile requires mission leases: one driver per loop and per mission, enforced at the write")
            if not isinstance(runtime.store, FencedEventStore):
                raise LoopError("the production profile requires the mission runtime's store to be a FencedEventStore, or a stale "
                                "driver could still append to a mission it no longer owns")
        self.rt, self.driver, self.verifier, self.leases = runtime, driver, verifier, leases
        self._raw = store
        self.store = FencedEventStore(store, leases) if leases is not None else store
        self.on_event, self.clock, self.owner = on_event or (lambda ev: None), clock, owner
        self._lock = threading.RLock()

    # ── log ──
    def state(self, loop_id: str) -> LoopState:
        return fold_loop([(e.type, e.payload, e.ts) for e in self._raw.for_mission(loop_id)], loop_id)

    def _emit(self, ev_type: str, loop_id: str, **extra) -> None:
        try:
            self.on_event({"type": ev_type, "loop_id": loop_id, "ts": self.clock(), **extra})
        except Exception:  # noqa: BLE001 - an observer must never change the loop
            pass

    def _append(self, loop_id: str, type_: str, payload: dict) -> None:
        """A CONTROLLER write: fenced, so a controller that lost its lease cannot write."""
        self.store.append(type_, loop_id, payload)

    def _owner_append(self, loop_id: str, type_: str, payload: dict) -> None:
        """A write recording an OWNER's decision (cancel, resolve an escalation, extend limits). It is an append-only statement of intent
        that must be possible from any process, so it does not need the controller's lease; the controller acts on it."""
        self._raw.append(type_, loop_id, payload)

    # ── lifecycle ──
    def start(self, spec: LoopSpec) -> str:
        if not str(spec.goal).strip():
            raise LoopError("a goal is required")
        lim = spec.limits
        if lim.max_iterations < 1 or (lim.max_usd is not None and not lim.max_usd > 0) or (lim.max_active_s is not None and not lim.max_active_s > 0):
            raise LoopError("hard limits must be positive: a loop without a ceiling is not allowed")
        loop_id = "loop_" + hashlib.sha256(f"{spec.goal}|{spec.tenant}|{time.time_ns()}".encode()).hexdigest()[:16]
        self._append(loop_id, "LoopStarted", {"loop_id": loop_id, "spec": {
            "goal": spec.goal, "tenant": spec.tenant, "owner": spec.owner or self.owner,
            "limits": lim.to_dict(), "soft": spec.soft.to_dict()}, "at": self.clock()})
        return loop_id

    def run(self, loop_id: str) -> LoopState:
        """Drive the loop until it completes, waits, escalates or hits a ceiling. Safe to call again at any time (after a restart, after a
        reconciliation, after an escalation is resolved): it continues from the log and never repeats finished work."""
        lease = None
        if self.leases is not None:
            lease = self.leases.acquire(loop_id, owner=self.owner or None)
            if lease is None:
                raise LoopBusy(f"{loop_id}: another controller holds this loop")
        try:
            if lease is not None:
                with lease:
                    return self._drive(loop_id)
            return self._drive(loop_id)
        finally:
            if lease is not None:
                lease.release()

    def tick(self, loop_ids: Optional[Iterable[str]] = None) -> dict:
        """One pass over every loop that is not finished (a scheduler calls this). A loop waiting on a reconciliation or a person is
        re-evaluated cheaply: if its mission moved (a probe or a person resolved it) the loop continues, otherwise nothing is written."""
        out = {}
        ids = list(loop_ids) if loop_ids is not None else [m for m in self._raw.mission_ids() if m.startswith("loop_")]
        for lid in ids:
            st = self.state(lid)
            if not st.started or st.terminal or st.status in ("escalated", "limit"):
                continue
            try:
                out[lid] = self.run(lid).status
            except LoopBusy:
                out[lid] = "busy"
        return out

    # ── owner decisions ──
    def resolve_escalation(self, loop_id: str, *, actor: str, decision: str, reason: str, guidance: Any = None, replans: int = 1) -> LoopState:
        """A person's decision on an escalated loop: `continue` (optionally with guidance; grants `replans` more replans and a fresh
        window), `stop` (cancels the loop). Names the actor and a reason, like a reconciliation."""
        self._need_actor(actor, reason)
        if decision not in ("continue", "stop"):
            raise LoopError("decision must be 'continue' or 'stop'")
        with self._lock:
            st = self.state(loop_id)
            if st.terminal:
                raise LoopError("this loop is finished")
            if st.escalation is None:
                raise LoopError("this loop has no open escalation")
            if decision == "stop":
                return self.cancel(loop_id, actor=actor, reason=reason)
            self._owner_append(loop_id, "EscalationResolved", {"decision": "continue", "actor": actor.strip(), "reason": reason.strip()[:500],
                                                          "guidance": guidance, "replans": max(0, int(replans)), "at": self.clock()})
        return self.run(loop_id)

    def extend_limits(self, loop_id: str, *, actor: str, reason: str, **limits) -> LoopState:
        """Raise hard ceilings (`max_iterations`, `max_active_s`, `max_usd`, `max_tokens`, `deadline`). Only the owner, named and with a
        reason; a ceiling is never raised by the loop itself."""
        self._need_actor(actor, reason)
        allowed = {"max_iterations", "max_active_s", "max_usd", "max_tokens", "deadline"}
        if not limits or set(limits) - allowed:
            raise LoopError(f"give one or more of {sorted(allowed)}")
        with self._lock:
            st = self.state(loop_id)
            if st.terminal:
                raise LoopError("this loop is finished")
            for k, v in limits.items():
                cur = st.limits.get(k)
                if v is None or (cur is not None and k != "deadline" and v < cur) or (k == "deadline" and cur is not None and v < cur):
                    raise LoopError(f"{k} can only be raised")
            self._owner_append(loop_id, "LimitsExtended", {"limits": limits, "actor": actor.strip(), "reason": reason.strip()[:500], "at": self.clock()})
        return self.run(loop_id)

    def cancel(self, loop_id: str, *, actor: str, reason: str) -> LoopState:
        """Record the intent first (a crash leaves it), then cancel the live mission. A call already in flight cannot be killed; the
        record lists what may remain (unresolved reconciliations) rather than implying a clean stop."""
        self._need_actor(actor, reason)
        with self._lock:
            st = self.state(loop_id)
            if st.terminal:
                return st
            if st.cancel_requested is None:
                self._owner_append(loop_id, "LoopCancelRequested", {"actor": actor.strip(), "reason": reason.strip()[:500], "at": self.clock()})
            return self._propagate_cancel(loop_id)

    # ── the drive loop ──
    def _drive(self, loop_id: str) -> LoopState:
        rt = self.rt
        while True:
            st = self.state(loop_id)
            if not st.started:
                raise LoopError(f"unknown loop {loop_id}")
            if st.terminal or st.status in ("escalated", "limit"):
                return st
            if st.cancel_requested is not None:
                return self._finish_cancel(loop_id)
            it = st.open_iteration
            if it is None:
                block = self._hard_limit(st, None)
                if block:
                    return self._block(loop_id, block)
                self._warn(loop_id, st, None)
                if self._unresolved(st):
                    return self._wait(loop_id, st, "waiting_reconciliation")      # never start a new attempt around an unknown
                it = self._begin(loop_id, st)
                continue
            m = rt._missions.get(it.mission_id) or rt.rehydrate(it.mission_id)
            if m.state not in _TERMINAL_MISSION:
                block = self._hard_limit(st, it)
                if block:
                    return self._block(loop_id, block)
                self._warn(loop_id, st, it)
                waiting = self._mission_wait(m)
                if waiting:
                    return self._wait(loop_id, st, waiting)
                if st.status != "running":
                    self._append(loop_id, "LoopWaiting", {"status": "running", "at": self.clock()})
                if not self._run_mission(loop_id, it, m):
                    return self.state(loop_id)
                continue
            self._finish_iteration(loop_id, st, it, m)
            st = self.state(loop_id)
            if st.terminal or st.status in ("escalated", "limit"):
                return st
            self._after_iteration(loop_id, st)

    def _begin(self, loop_id: str, st: LoopState) -> Iteration:
        n = len(st.iterations) + 1
        ctx = self._ctx(loop_id, st, n)
        mid = self.driver.start(ctx)
        if not mid or mid not in self.rt._missions:
            raise LoopError("the driver must create the mission for the iteration and return its id")
        self._append(loop_id, "IterationStarted", {"n": n, "mission_id": mid, "key": ctx.key, "at": self.clock()})
        return self.state(loop_id).open_iteration

    def _ctx(self, loop_id: str, st: LoopState, n: int, diagnosis: Optional[dict] = None) -> IterationContext:
        last = next((i.failure_sig for i in reversed(st.iterations) if i.finished and i.failure_sig), "")
        return IterationContext(loop_id=loop_id, n=n, goal=st.spec.get("goal", ""), guidance=tuple(g for g in st.guidance if g is not None),
                                completed=frozenset(st.progress_seen), last_failure=last, diagnosis=diagnosis)

    def _mission_wait(self, m) -> str:
        if self.rt.repo.pending_reconciliation(m.id) and not self.rt._independent_work(m.id, self.rt._plans[m.id]):
            return "waiting_reconciliation"
        if m.state is MissionState.WAITING_RECONCILIATION:
            return "waiting_reconciliation"
        if m.state is MissionState.WAITING_HUMAN or self.rt.repo.pending_human(m.id) is not None:
            return "waiting_human"
        if m.state is MissionState.PAUSED:
            return "waiting_human"
        return ""

    def _unresolved(self, st: LoopState) -> bool:
        return any(self.rt.repo.pending_reconciliation(i.mission_id) for i in st.iterations)

    def _run_mission(self, loop_id: str, it: Iteration, m) -> bool:
        """Run the iteration's mission under its own fenced lease. False when the loop must stop here (the mission is owned elsewhere)."""
        lease = None
        if self.leases is not None:
            lease = self.leases.acquire(m.id, owner=self.owner or None)
            if lease is None:
                self._wait(loop_id, self.state(loop_id), "waiting_human", detail="the mission is driven by another owner")
                return False
        t0 = time.monotonic()
        try:
            if lease is not None:
                with lease:
                    self.rt.run(m.id)
            else:
                self.rt.run(m.id)
        finally:
            try:                                          # durable, so a restart cannot reset the active-time budget
                self._append(loop_id, "SegmentFinished", {"n": it.n, "s": round(time.monotonic() - t0, 3), "at": self.clock()})
            finally:
                if lease is not None:
                    lease.release()
        return True

    # ── iteration outcome ──
    def _outcome(self, m) -> tuple:
        o = m.outcome or {}
        if m.state is MissionState.SUCCEEDED:
            return "succeeded", ""
        if m.state is MissionState.CANCELLED:
            return "cancelled", "the mission was cancelled"
        if m.state is MissionState.COMPENSATION_FAILED:
            return "needs_attention", "a forward effect remains after a failed compensation"
        if o.get("blocked") == "hard_limit":
            return "hard_limit", f"mission limit {o.get('limit')} reached"
        if o.get("blocked") == "authority_denied":
            return "authority_denied", str(o.get("reason") or o.get("rule") or "authority denied")
        if any(r.get("reversibility") == "UNKNOWN_EFFECT" for r in (o.get("not_reversible") or []) if isinstance(r, dict)):
            return "needs_attention", "an effect may have been applied and cannot be confirmed"
        return "failed", str(o.get("reason") or o.get("blocked") or "failed")

    def _finish_iteration(self, loop_id: str, st: LoopState, it: Iteration, m) -> None:
        rt = self.rt
        outcome, why = self._outcome(m)
        usd, tok = _cost(rt, m.id)
        status = rt.repo.node_status(m.id)
        failed = sorted({n.capability for n in rt._plans[m.id].graph.nodes if status.get(n.id) == NodeState.FAILED})
        sig = _sha([outcome, why if outcome in ("authority_denied", "hard_limit") else "", failed]) if outcome != "succeeded" else ""
        if outcome == "succeeded":
            v = self._verify(loop_id, st, it, m)
            if v is not None and v.verified:
                self._record_iteration(loop_id, it, m, "succeeded", usd, tok, "")
                self._append(loop_id, "GoalVerified", {"n": it.n, "evidence": v.evidence, "reason": v.reason, "at": self.clock()})
                self._append(loop_id, "LoopCompleted", {"verified": True, "n": it.n, "evidence": v.evidence, "at": self.clock()})
                self._emit("LOOP_COMPLETED", loop_id, verified=True, iterations=it.n)
                return
            if v is None:
                self._record_iteration(loop_id, it, m, "succeeded", usd, tok, "")
                self._append(loop_id, "LoopCompleted", {"verified": False, "n": it.n, "at": self.clock(),
                                                        "note": "no verifier is configured: the goal is not verified"})
                self._emit("LOOP_COMPLETED", loop_id, verified=False, iterations=it.n)
                return
            outcome, why = "unverified", v.reason or "the verifier did not confirm the goal"
            sig = _sha(["unverified", why])
        self._record_iteration(loop_id, it, m, outcome, usd, tok, sig, why)

    def _record_iteration(self, loop_id, it: Iteration, m, outcome, usd, tok, sig, why="") -> None:
        self._append(loop_id, "IterationFinished", {
            "n": it.n, "mission_id": m.id, "outcome": outcome, "why": why[:300], "usd": usd, "tokens": tok,
            "failure_sig": sig, "plan_sig": _plan_sig(self.rt, m.id),
            "done_sigs": _step_sigs(self.rt, m.id), "at": self.clock()})

    def _verify(self, loop_id: str, st: LoopState, it: Iteration, m) -> Optional[Verification]:
        if self.verifier is None:
            return None
        try:
            v = self.verifier(self._ctx(loop_id, st, it.n), m)
        except Exception as e:  # noqa: BLE001 - "I could not check" is never "verified"
            return Verification(False, None, f"the verifier failed: {type(e).__name__}: {str(e)[:150]}")
        if not isinstance(v, Verification):
            return Verification(False, None, "the verifier returned no structured result")
        if v.verified:
            try:
                ev = validate_evidence(v.evidence)
            except ReconciliationError as e:
                return Verification(False, None, f"a verification without usable evidence is not a verification: {e}")
            return replace(v, evidence=ev)
        return v

    # ── soft thresholds: diagnose -> replan -> escalate ──
    def _after_iteration(self, loop_id: str, st: LoopState) -> None:
        last = st.iterations[-1]
        if last.outcome == "cancelled":
            self._append(loop_id, "LoopCancelRequested", {"actor": "mission", "reason": "the iteration's mission was cancelled", "at": self.clock()})
            return
        if last.outcome in ("authority_denied", "hard_limit", "needs_attention"):
            # decisions for a person, never obstacles to plan around
            self._escalate(loop_id, kind=last.outcome, detail=self._last_why(loop_id, last),
                           evidence={"iteration": last.n, "mission_id": last.mission_id})
            return
        if self._unresolved(st):
            return                  # an effect of this iteration is unknown: no diagnosis, no replan, no new attempt until it is reconciled
        diag = self._diagnose(st)
        if diag is None:
            return
        self._append(loop_id, "Diagnosed", {**diag, "at": self.clock()})
        self._emit("LOOP_STALLED", loop_id, **diag)
        if st.replan_allowance > 0:
            g = self.driver.replan(self._ctx(loop_id, st, len(st.iterations) + 1, diag))
            if g is not None:
                self._append(loop_id, "Replanned", {"guidance": g, "diagnosis": diag, "n": len(st.iterations), "at": self.clock()})
                return
        self._escalate(loop_id, kind=diag["kind"], detail="the loop could not find a different approach within its replan budget"
                       if st.replan_allowance <= 0 else "the driver has no different approach to offer", evidence=diag)

    def _last_why(self, loop_id: str, it: Iteration) -> str:
        for e in reversed(self._raw.for_mission(loop_id)):
            if e.type == "IterationFinished" and int(e.payload.get("n", -1)) == it.n:
                return e.payload.get("why", "")
        return ""

    def _diagnose(self, st: LoopState) -> Optional[dict]:
        soft, w = st.spec.get("soft") or {}, st.window()
        if not w:
            return None
        repeat_n, stall_n, plan_n = int(soft.get("repeat_failures", 2)), int(soft.get("stall_iterations", 3)), int(soft.get("plan_repeats", 2))
        counts: dict = {}
        for i in w:
            if i.failure_sig:
                counts[i.failure_sig] = counts.get(i.failure_sig, 0) + 1
        sig, n = max(counts.items(), key=lambda kv: kv[1], default=("", 0))
        streak = 0
        for i in reversed(w):
            if i.progress == 0:
                streak += 1
            else:
                break
        if n >= repeat_n:
            return {"kind": "repeating_failure", "evidence": {"failure_sig": sig, "times": n, "iterations": [i.n for i in w if i.failure_sig == sig]}}
        plans: dict = {}
        for i in w:                                     # repeats of a plan that added nothing new
            if i.plan_sig and i.progress == 0:
                plans[i.plan_sig] = plans.get(i.plan_sig, 0) + 1
        psig, pn = max(plans.items(), key=lambda kv: kv[1], default=("", 0))
        if pn >= plan_n:
            return {"kind": "plan_cycling", "evidence": {"plan_sig": psig, "times": pn}}
        if streak >= stall_n:
            return {"kind": "no_progress", "evidence": {"iterations_without_progress": streak}}
        return None

    def _escalate(self, loop_id: str, *, kind: str, detail: str, evidence: dict) -> None:
        self._append(loop_id, "LoopEscalated", {"kind": kind, "detail": str(detail)[:300], "evidence": evidence, "at": self.clock()})
        self._emit("LOOP_ESCALATED", loop_id, kind=kind, detail=detail)

    # ── hard limits ──
    def _spent(self, st: LoopState, it: Optional[Iteration]) -> dict:
        usd = sum(i.usd for i in st.iterations if i.finished)
        tok = sum(i.tokens for i in st.iterations if i.finished)
        act = sum(i.active_s for i in st.iterations)
        if it is not None:
            u, t = _cost(self.rt, it.mission_id)
            usd, tok = usd + u, tok + t
        return {"iterations": len(st.iterations), "active_s": act, "usd": usd, "tokens": tok}

    def _caps(self, st: LoopState) -> dict:
        lim = st.limits
        return {"iterations": lim.get("max_iterations"), "active_s": lim.get("max_active_s"), "usd": lim.get("max_usd"), "tokens": lim.get("max_tokens")}

    def _hard_limit(self, st: LoopState, it: Optional[Iteration]) -> Optional[dict]:
        now = self.clock()
        dl = st.limits.get("deadline")
        if dl is not None and now >= float(dl):
            return {"limit": "deadline", "used": round(now, 3), "cap": float(dl)}
        used = self._spent(st, it)
        for name, cap in self._caps(st).items():
            if cap is None:
                continue
            if name == "iterations":
                over = it is None and used["iterations"] >= cap          # about to start one more than the cap allows
            elif name == "active_s":
                over = used[name] >= cap
            else:                                                        # money and tokens: no headroom left before a new iteration
                over = used[name] >= cap - 1e-9 if it is None else used[name] > cap + 1e-9
            if over:
                return {"limit": name, "used": used[name], "cap": cap}
        return None

    def _warn(self, loop_id: str, st: LoopState, it: Optional[Iteration]) -> None:
        frac = float((st.spec.get("soft") or {}).get("warn_fraction", 0.8))
        used = self._spent(st, it)
        for name, cap in self._caps(st).items():
            if cap and name not in st.warned and used[name] >= frac * cap:
                self._append(loop_id, "BudgetNearLimit", {"cap": name, "used": used[name], "limit": cap, "fraction": frac, "at": self.clock()})
                self._emit("BUDGET_NEAR_LIMIT", loop_id, cap=name, used=used[name], limit=cap)

    def _block(self, loop_id: str, block: dict) -> LoopState:
        self._append(loop_id, "LoopLimitReached", {**block, "at": self.clock()})
        self._emit("LOOP_LIMIT_REACHED", loop_id, **block)
        return self.state(loop_id)

    # ── waiting, cancelling ──
    def _wait(self, loop_id: str, st: LoopState, status: str, detail: str = "") -> LoopState:
        if st.status != status:                         # a waiting loop writes nothing more while it waits
            self._append(loop_id, "LoopWaiting", {"status": status, "detail": detail, "at": self.clock()})
        return self.state(loop_id)

    def _propagate_cancel(self, loop_id: str) -> LoopState:
        """Cancel the live mission and close the loop, if this process may write the loop (it holds, or can take, the lease). Otherwise
        the intent stays on the record and the controller that owns the loop finishes it at its next boundary."""
        if self.leases is None or loop_id in self.leases.held:
            return self._finish_cancel(loop_id)
        lease = self.leases.acquire(loop_id, owner=self.owner or None)
        if lease is None:
            return self.state(loop_id)
        try:
            return self._finish_cancel(loop_id)
        finally:
            lease.release()

    def _finish_cancel(self, loop_id: str) -> LoopState:
        st = self.state(loop_id)
        if st.terminal:
            return st
        pending = []
        for it in st.iterations:
            m = self.rt._missions.get(it.mission_id)
            if m is not None and m.state not in _TERMINAL_MISSION:
                if not self._cancel_mission(it.mission_id, loop_id):
                    return self.state(loop_id)           # its owner holds it: the intent is recorded, they finish at their boundary
            pending += [{"mission_id": it.mission_id, **p} for p in self.rt.repo.pending_reconciliation(it.mission_id)]
        req = st.cancel_requested or {}
        self._append(loop_id, "LoopCancelled", {"actor": req.get("actor", ""), "reason": req.get("reason", ""),
                                                "unresolved_reconciliations": [{k: p.get(k) for k in ("mission_id", "node_id", "lease_id")} for p in pending],
                                                "at": self.clock()})
        self._emit("LOOP_CANCELLED", loop_id, unresolved=len(pending))
        return self.state(loop_id)

    def _cancel_mission(self, mission_id: str, loop_id: str) -> bool:
        """Cancel one mission, under its fenced lease when leases are in use. False if another owner holds it."""
        lease = None
        if self.leases is not None and mission_id not in self.leases.held:
            lease = self.leases.acquire(mission_id, owner=self.owner or None)
            if lease is None:
                return False
        try:
            self.rt.cancel_mission(mission_id, reason=f"loop {loop_id} cancelled")
            return True
        finally:
            if lease is not None:
                lease.release()

    @staticmethod
    def _need_actor(actor: str, reason: str) -> None:
        if not str(actor).strip():
            raise LoopError("the person deciding must be named")
        if len(str(reason).strip()) < 8:
            raise LoopError("a reason of at least a few words is required")


# ───────────────────────────── wait-state classification ─────────────────────────────
def classify_loop(st: LoopState, *, now: Optional[float] = None, hung_after_s: float = 900.0):
    """Why is this loop not progressing? A stalled loop is the controller's own case (it diagnoses and replans); only a RUNNING loop with
    nothing recorded for too long is a hung one a watchdog may touch."""
    from .waitstate import Assessment, Wait  # noqa: PLC0415
    now = time.time() if now is None else now
    if st.terminal:
        return Assessment(Wait.TERMINAL, st.status)
    if st.status == "escalated":
        return Assessment(Wait.WAITING_ESCALATED, (st.escalation or {}).get("kind", ""))
    if st.status == "limit":
        return Assessment(Wait.WAITING_ESCALATED, f"hard limit {(st.limit_block or {}).get('limit', '')}: the owner decides")
    if st.status == "waiting_reconciliation":
        return Assessment(Wait.WAITING_OBSERVATION, "an iteration's unknown outcome is being reconciled")
    if st.status == "waiting_human":
        return Assessment(Wait.WAITING_HUMAN, "an iteration waits for a person")
    if st.last_diagnosis and st.window_from <= len(st.iterations) and st.iterations and st.iterations[-1].finished:
        return Assessment(Wait.STALLED, (st.last_diagnosis or {}).get("kind", ""))
    if st.last_ts and now - st.last_ts >= hung_after_s:
        return Assessment(Wait.HUNG, f"running, but nothing has been recorded for {int(now - st.last_ts)}s")
    return Assessment(Wait.ACTIVE)
