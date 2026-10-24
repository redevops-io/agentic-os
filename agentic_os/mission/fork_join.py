"""Mission fork/join — parallel branches *within* an existing Mission (docs/mission-execution.md).

A fork is NOT a set of independently authorized agents. The parent Mission owns authority, budget, deadline
and cancellation; a branch is one capability invocation that receives only the capability it declares and
can never approve anything. State is a pure fold over durable events (``fold_fork``); branch outcomes and
execution receipts are persisted BEFORE the join is evaluated, and a restart resumes from the log without
re-executing finished branches (in-flight branches are re-dispatched with the SAME idempotency key).

This module owns the pure model (specs, events, fold, join evaluation) and ``ForkJoinController``, which
drives a fork through the runtime's existing seams (``_pre_exec_ok``, ``_meter``, ``_executor_for``,
policy plane, verifier, concurrency conflict keys). ``MissionRuntime`` only exposes it via ``.forks`` and
routes branch approvals to it from ``approve()``.
"""
from __future__ import annotations

import concurrent.futures as _cf
import copy
import hashlib
import json
import math
import os
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import concurrency as conc
from .approval import is_approval_key, strip_approval_keys
from . import compensation as _comp
from . import compensation_policy as _cpol
from .context import CHECK_POLICY, ContextIntent
from .lifecycle import GateReached
from .types import HumanTask, Mission, MissionState, Node, NodeCost, NodeState, VerificationDecision
from .verify import is_staged, needs_verification

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_RESERVED_INPUT_KEYS = frozenset({"_goal", "_mission", "_node"})
_TERMINAL_BRANCH = frozenset({"succeeded", "failed", "cancelled"})
MODES = ("all", "any", "quorum")


class ForkError(Exception):
    """A fork spec was refused (validation, limits, capability not granted, spec drift). Nothing ran."""


# ───────────────────────────── identity ─────────────────────────────
def branch_node_id(mission_id: str, fork_id: str, branch_id: str) -> str:
    """The branch's ``Node.id``. Embeds mission, fork and branch so an approval token bound to this node id
    (approval.py, fail-closed binding) cannot run any other branch, fork or mission."""
    return f"{mission_id}:fork:{fork_id}:{branch_id}"


def branch_idempotency_key(mission_id: str, fork_id: str, branch_id: str, attempt: int) -> str:
    h = hashlib.sha256(f"{mission_id}|{fork_id}|{branch_id}|{int(attempt)}".encode()).hexdigest()
    return "fj-" + h[:32]


def is_branch_node_id(node_id: str) -> bool:
    return isinstance(node_id, str) and ":fork:" in node_id


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj)).hexdigest()


# ───────────────────────────── specs ─────────────────────────────
@dataclass
class BranchSpec:
    branch_id: str
    capability: str
    inputs: dict = field(default_factory=dict)
    requires: tuple = ()               # extra capabilities the branch may name; must be a subset of mission grants
    produces: str | None = None        # world key published (after the join) from this branch's result
    max_attempts: int = 1              # retries only for non-side-effecting capabilities (or not_applied failures)


@dataclass
class JoinSpec:
    mode: str = "all"                  # all | any | quorum
    k: int | None = None               # quorum size (1 <= k <= n)
    timeout_s: float | None = None
    on_timeout: str = "fail"           # fail | partial
    cancel_losers: bool = True         # any/quorum: cancel still-running branches once satisfied (else drain)


@dataclass
class ForkSpec:
    fork_id: str
    branches: list = field(default_factory=list)
    join: JoinSpec = field(default_factory=JoinSpec)
    max_concurrent: int | None = None
    stricter_failure_policy: str | None = None   # may only be HALT_AND_REPORT or equal to the mission's policy
    cancel_siblings_on_branch_cancel: bool = False

    def digest(self) -> str:
        return _sha({
            "fork_id": self.fork_id,
            "branches": [{"id": b.branch_id, "cap": b.capability, "requires": sorted(b.requires),
                          "produces": b.produces, "inputs_sha256": _sha(b.inputs),
                          "max_attempts": b.max_attempts} for b in self.branches],
            "join": vars(self.join), "max_concurrent": self.max_concurrent,
            "stricter_failure_policy": self.stricter_failure_policy,
            "csbc": self.cancel_siblings_on_branch_cancel})


# ───────────────────────────── limits ─────────────────────────────
_LIMIT_KEYS = ("max_concurrent_branches", "max_branches", "max_open_forks")


@dataclass
class ForkLimits:
    max_concurrent_branches: int = 4
    max_branches: int = 16
    max_open_forks: int = 4
    per_tenant: dict = field(default_factory=dict)      # tenant -> {key: int}

    @classmethod
    def from_env(cls) -> "ForkLimits":
        lim = cls()
        for key, env in (("max_concurrent_branches", "AGENTIC_OS_FORK_MAX_CONCURRENT"),
                         ("max_branches", "AGENTIC_OS_FORK_MAX_BRANCHES"),
                         ("max_open_forks", "AGENTIC_OS_FORK_MAX_OPEN")):
            try:
                val = int(os.environ.get(env, "").strip())
            except ValueError:
                continue
            if val > 0:
                setattr(lim, key, val)
        raw = os.environ.get("AGENTIC_OS_FORK_TENANT_LIMITS", "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    lim.per_tenant = {str(t): dict(v) for t, v in parsed.items() if isinstance(v, dict)}
            except ValueError:
                pass
        return lim

    def for_tenant(self, tenant: str) -> dict:
        out = {k: getattr(self, k) for k in _LIMIT_KEYS}
        for k, v in (self.per_tenant.get(tenant or "") or {}).items():
            if k in out and isinstance(v, int) and not isinstance(v, bool) and v > 0:
                out[k] = v
        return out


# ───────────────────────────── failure policy hook ─────────────────────────────
@dataclass
class FailureDecision:
    """What the Mission's failure policy decided about a failed join (mapped explicitly from #450's
    ``FailureAction``: HALT -> halt_and_report, COMPENSATE -> compensate, CONTINUE -> continue)."""
    action: str = "halt_and_report"    # halt_and_report | compensate | continue
    note: str = ""
    policy: str = "HALT_AND_REPORT"
    compensate_nodes: tuple = ()       # branch node ids whose DECLARED undo should run (reverse order)
    not_reversible: tuple = ()         # succeeded consequential branches that cannot be undone
    not_compensated: tuple = ()        # reversible succeeded branches left as-is by policy

    def to_dict(self) -> dict:
        return {"action": self.action, "note": self.note, "policy": self.policy,
                "compensate_nodes": list(self.compensate_nodes), "not_reversible": list(self.not_reversible),
                "not_compensated": list(self.not_compensated)}


_ACTION_MAP = {"HALT": "halt_and_report", "COMPENSATE": "compensate", "CONTINUE": "continue"}


@dataclass
class FailureContext:
    mission_id: str
    fork_id: str
    join_mode: str
    reason: str
    succeeded: list                    # [{"branch_id","node_id","capability","receipt"}]
    failed: list
    cancelled: list
    tenant: str = ""
    policy: str = "HALT_AND_REPORT"


class _PolicyView:
    """The mission-level failure policy as recorded in ForkOpened (replay-stable), as #450 expects it."""

    def __init__(self, failure_policy: str):
        self.failure_policy = failure_policy


_STRICT_OK = "HALT_AND_REPORT"


# ───────────────────────────── fold ─────────────────────────────
@dataclass
class BranchState:
    branch_id: str
    node_id: str = ""
    capability: str = ""
    operator: str = ""
    produces: str | None = None
    max_attempts: int = 1
    status: str = "pending"            # pending|running|parked|reconciling|retrying|succeeded|failed|cancelled
    attempt: int = 0
    idempotency_key: str = ""
    reason: str = ""
    ran: bool = False
    result: Any = None
    receipt: dict | None = None
    approval: str | None = None        # capability an unrevoked human approval is bound to
    parked: bool = False
    reserved: dict = field(default_factory=dict)
    generation: int = 0
    awaiting: bool = False             # an unknown outcome is unresolved (ReconciliationRequired minus ReconciliationResolved)
    reconcile: dict | None = None      # that record: lease_id, attempt, reason

    def to_dict(self) -> dict:
        d = {"branch_id": self.branch_id, "node_id": self.node_id, "capability": self.capability,
             "status": self.status, "attempt": self.attempt, "idempotency_key": self.idempotency_key,
             "reason": self.reason, "ran": self.ran, "result": self.result, "receipt": self.receipt,
             "approval": self.approval, "parked": self.parked}
        if self.awaiting:                  # omitted when false: the serialised form of every existing fork is unchanged
            d["awaiting_reconciliation"] = True
        return d


@dataclass
class ForkState:
    fork_id: str
    opened: bool = False
    spec_digest: str = ""
    join: dict = field(default_factory=dict)
    failure_policy: str | None = None
    csbc: bool = False
    limits: dict = field(default_factory=dict)
    opened_at: float = 0.0
    deadline: float | None = None
    order: list = field(default_factory=list)
    branches: dict = field(default_factory=dict)
    success_order: list = field(default_factory=list)
    late_results: list = field(default_factory=list)
    closed: str | None = None           # None | "satisfied" | "failed"
    close_payload: dict = field(default_factory=dict)
    rejected: str | None = None
    claim: dict = field(default_factory=dict)   # the live driver claim, if any
    generation: int = 0                 # highest ForkDriverClaimed generation (fencing token)

    def ordered(self) -> list:
        return [self.branches[b] for b in self.order]

    def ids(self, *statuses: str) -> list:
        return [b.branch_id for b in self.ordered() if b.status in statuses]

    def counts(self) -> dict:
        c = {"succeeded": 0, "failed": 0, "cancelled": 0}
        for b in self.ordered():
            if b.status in c:
                c[b.status] += 1
        c["n"] = len(self.order)
        c["remaining"] = c["n"] - c["succeeded"] - c["failed"] - c["cancelled"]
        return c

    def to_dict(self) -> dict:
        return {"fork_id": self.fork_id, "opened": self.opened, "spec_digest": self.spec_digest,
                "join": self.join, "closed": self.closed, "close_payload": self.close_payload,
                "branches": [b.to_dict() for b in self.ordered()],
                "success_order": list(self.success_order),
                "late_results": copy.deepcopy(self.late_results)}


def fold_fork(events: Iterable, fork_id: str) -> ForkState:
    """Pure fold of the mission's events for one fork. ``events`` yields objects with ``.type`` / ``.payload``
    (store ``Event``) or ``(type, payload)`` tuples. No clock, no randomness: same log -> same state."""
    st = ForkState(fork_id=fork_id)
    by_node: dict[str, str] = {}
    for ev in events:
        t, p = (ev if isinstance(ev, tuple) else (ev.type, ev.payload))
        p = p or {}
        if t == "ForkOpened" and p.get("fork_id") == fork_id and not st.opened:
            st.opened, st.spec_digest = True, p.get("spec_digest", "")
            st.join, st.failure_policy = dict(p.get("join") or {}), p.get("failure_policy")
            st.csbc, st.limits = bool(p.get("cancel_siblings_on_branch_cancel")), dict(p.get("limits") or {})
            st.opened_at, st.deadline = float(p.get("opened_at") or 0.0), p.get("deadline")
            for b in p.get("branches") or []:
                bs = BranchState(branch_id=b["branch_id"], node_id=b.get("node_id", ""),
                                 capability=b.get("capability", ""), operator=b.get("operator", ""),
                                 produces=b.get("produces"), max_attempts=int(b.get("max_attempts", 1)))
                st.order.append(bs.branch_id)
                st.branches[bs.branch_id] = bs
                by_node[bs.node_id] = bs.branch_id
            continue
        if t == "ForkRejected" and p.get("fork_id") == fork_id:
            st.rejected = p.get("reason", "")
            continue
        if t == "ForkDriverClaimed" and p.get("fork_id") == fork_id:
            st.generation = max(st.generation, int(p.get("generation", 0) or 0))
            st.claim = dict(p)
            continue
        if t == "ForkDriverReleased" and p.get("fork_id") == fork_id:
            if st.claim and st.claim.get("owner") == p.get("owner"):
                st.claim = {}
            continue
        if not st.opened:
            continue
        if t in ("NodeParked", "NodeResumed", "NodeSkipped", "ApprovalGranted", "ApprovalInvalidated",
                 "ApprovalRejected", "ReconciliationRequired", "ReconciliationResolved"):
            bid = by_node.get(p.get("node_id", ""))
            if bid is None:
                continue
            b = st.branches[bid]
            if t == "NodeParked":
                b.parked = True
            elif t in ("NodeResumed", "NodeSkipped"):
                b.parked = False
            elif t == "ApprovalGranted":
                b.approval = p.get("capability") or ""
            elif t == "ApprovalInvalidated":
                b.approval = None
            elif t == "ReconciliationRequired":
                b.awaiting, b.reconcile = True, dict(p)
            elif t == "ReconciliationResolved":
                b.awaiting = False
            continue
        if p.get("fork_id") != fork_id:
            continue
        b = st.branches.get(p.get("branch_id", "")) if t.startswith("Branch") else None
        closed = st.closed is not None
        if t == "BranchStarted" and b is not None:
            if b.status in _TERMINAL_BRANCH or closed:
                continue
            b.status, b.attempt = "running", int(p.get("attempt", 1))
            b.idempotency_key, b.ran = p.get("idempotency_key", ""), True
            b.generation = int(p.get("generation", 0) or 0)
            if p.get("reserved"):
                b.reserved = dict(p["reserved"])
        elif t == "BranchSucceeded" and b is not None:
            if p.get("late") or b.status in _TERMINAL_BRANCH or closed:
                st.late_results.append({"branch_id": b.branch_id, "attempt": p.get("attempt"),
                                        "receipt": p.get("receipt"), "result": p.get("result")})
                continue
            b.status, b.result, b.receipt, b.reason = "succeeded", p.get("result"), p.get("receipt"), ""
            st.success_order.append(b.branch_id)
        elif t == "BranchFailed" and b is not None:
            if b.status in _TERMINAL_BRANCH or closed:
                continue
            b.reason, b.ran = p.get("reason", ""), bool(p.get("ran"))
            b.result, b.receipt = p.get("result"), p.get("receipt")
            b.status = "retrying" if p.get("will_retry") else "failed"
        elif t == "BranchCancelled" and b is not None:
            if b.status in _TERMINAL_BRANCH or closed:
                continue
            b.status, b.reason = "cancelled", p.get("reason", "")
        elif t == "JoinSatisfied" and not closed:
            st.closed, st.close_payload = "satisfied", dict(p)
        elif t == "JoinFailed" and not closed:
            st.closed, st.close_payload = "failed", dict(p)
    for b in st.branches.values():     # derived, not stored: a parked non-terminal branch shows as parked
        if b.parked and b.status in ("pending", "retrying"):
            b.status = "parked"
        if b.awaiting and b.status in ("pending", "running", "parked"):
            b.status = "reconciling"   # a dispatched call whose outcome is unknown: neither running, failed nor retryable
    return st


def fork_ids(events: Iterable) -> list:
    """Fork ids opened in the log, in log order."""
    out: list[str] = []
    for ev in events:
        t, p = (ev if isinstance(ev, tuple) else (ev.type, ev.payload))
        if t == "ForkOpened" and (p or {}).get("fork_id") not in out:
            out.append(p["fork_id"])
    return out


def fold_forks(events: Iterable) -> dict:
    evs = list(events)
    return {fid: fold_fork(evs, fid) for fid in fork_ids(evs)}


def evaluate_join(mode: str, k: int | None, n: int, s: int, f: int, c: int) -> str:
    """Pure join verdict from terminal counts: "satisfied" | "failed" | "pending"."""
    r = n - s - f - c
    if mode == "all":
        if s == n:
            return "satisfied"
        return "failed" if (f + c) > 0 else "pending"
    need = 1 if mode == "any" else int(k or 1)
    if s >= need:
        return "satisfied"
    return "failed" if s + r < need else "pending"


def _winners(st: ForkState) -> list:
    mode, k = st.join.get("mode", "all"), st.join.get("k")
    ok = [b.branch_id for b in st.ordered() if b.status == "succeeded"]
    if mode == "all":
        return ok
    if mode == "any":
        return st.success_order[:1]
    return ok[: int(k or 1)]


# ───────────────────────────── controller ─────────────────────────────
@dataclass
class ForkResult:
    status: str                     # satisfied | failed | parked | awaiting_reconciliation
    state: ForkState

    @property
    def results(self) -> dict:
        return {b.branch_id: b.result for b in self.state.ordered() if b.status == "succeeded"}


class Superseded(ForkError):
    """This driver's claim was superseded by a higher generation: it must stop dispatching and never close."""


class SimulatedCrash(BaseException):
    """Raised by test harnesses to model a process crash; never swallowed by the controller."""


_DRIVER_LOCKS: dict = {}
_DRIVER_LOCKS_GUARD = threading.Lock()
_LIVE_OWNERS: set = set()             # driver claims whose drive loop is running in THIS process
LEASE_S = 60.0
_HOST = socket.gethostname()


def _driver_lock(store, mission_id: str, fork_id: str) -> threading.Lock:
    """Process-wide single-driver lock per (log, mission, fork): two controllers/threads on one log serialize."""
    path = getattr(store, "_path", None)
    logkey = os.path.realpath(path) if path else id(store)
    with _DRIVER_LOCKS_GUARD:
        return _DRIVER_LOCKS.setdefault((logkey, mission_id, fork_id), threading.Lock())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


class _FileLock:
    """Cross-process mutex around claim/refresh (flock on a sidecar file) for file-backed logs."""

    def __init__(self, store):
        path = getattr(store, "_path", None)
        self.path = (path + ".forklock") if path else None
        self.fh = None

    def __enter__(self):
        if self.path:
            import fcntl  # noqa: PLC0415
            self.fh = open(self.path, "a+")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        if self.fh:
            import fcntl  # noqa: PLC0415
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


class ForkJoinController:
    def __init__(self, runtime, *, limits: ForkLimits | None = None, clock: Callable[[], float] = time.time,
                 poll_s: float = 0.005):
        self.rt = runtime
        self.limits = limits or ForkLimits.from_env()
        self.clock = clock
        self.poll_s = poll_s
        self._lock = threading.RLock()
        self._specs: dict[tuple, ForkSpec] = {}
        self._driving: set = set()
        self._tenant_slots: dict[str, int] = {}
        self._hook: Callable | None = None
        self._gen: dict = {}
        self._own: dict = {}
        self._cancelled: set = set()
        self._tenants: dict[str, str] = {}
        runtime.store.subscribe(self._on_event)

    # ── public hooks ──
    def on_branch_failure(self, hook: Callable) -> None:
        """Optional extra hook ``hook(FailureContext, FailureDecision) -> FailureDecision`` called after the
        MISSION's failure policy (``compensation_policy.decide_on_branch_failure``) decided. It can only
        TIGHTEN the decision (return halt_and_report, or the same action with a subset of compensate_nodes);
        anything looser, an error, or a wrong type is ignored. The mission policy stays the single source."""
        self._hook = hook

    def register_spec(self, mission_id: str, spec: ForkSpec) -> None:
        """After a restart the spec (inputs are not in the log) must be re-supplied; this validates it against
        the logged ``spec_digest`` (spec_drift refused) so a later ``approve()`` can resume the fork."""
        st = self.state(mission_id, spec.fork_id)
        if not st.opened:
            raise ForkError(f"fork {spec.fork_id} was never opened in mission {mission_id}")
        if st.spec_digest != spec.digest():
            raise ForkError(f"spec_drift: fork {spec.fork_id} spec differs from the recorded spec_digest")
        self._specs[(mission_id, spec.fork_id)] = spec

    def state(self, mission_id: str, fork_id: str) -> ForkState:
        return fold_fork(self.rt.store.for_mission(mission_id), fork_id)

    def owns(self, mission_id: str, node_id: str) -> bool:
        if not is_branch_node_id(node_id):
            return False
        evs = self.rt.store.for_mission(mission_id)
        return any(node_id == branch_node_id(mission_id, fid, b)
                   for fid in fork_ids(evs) for b in fold_fork(evs, fid).order)

    # ── open / run ──
    def run_fork(self, mission_id: str, spec: ForkSpec) -> ForkResult:
        """Open (first call) or resume (any later call, e.g. after a restart) the fork and drive it until it
        closes or parks on a human approval. Idempotent: a closed fork returns its stored result. At most one
        driver runs a fork: concurrent callers in this process serialize on a process-wide lock, and a driver
        in another process is refused via a durable, fenced ``ForkDriverClaimed`` lease."""
        with _driver_lock(self.rt.store, mission_id, str(getattr(spec, "fork_id", ""))):
            return self._run_locked(mission_id, spec)

    def _run_locked(self, mission_id: str, spec: ForkSpec) -> ForkResult:
        rt = self.rt
        m = rt._missions[mission_id]
        refresh = getattr(rt.store, "refresh", None)
        if refresh:
            refresh()
        done = self.state(mission_id, spec.fork_id)
        if done.closed:                      # already decided: only re-apply the (idempotent) mission effects
            if done.spec_digest != spec.digest():
                raise ForkError(f"spec_drift: fork {spec.fork_id} spec differs from the recorded spec_digest")
            self._apply_close(m, done)
            return ForkResult(done.closed, done)
        if m.state in (MissionState.CANCELLED, MissionState.FAILED, MissionState.SUCCEEDED,
                       MissionState.COMPENSATION_FAILED):
            raise ForkError(f"mission {mission_id} is {m.state.value}; cannot run a fork")
        nodes = self._open(m, spec)
        self._specs[(mission_id, spec.fork_id)] = spec
        gen, owner = self._claim(m, spec.fork_id)
        try:
            return self._drive(m, spec, nodes, gen, owner)
        finally:
            self._release(m, spec.fork_id, gen, owner)

    # ── driver lease (fencing) ──
    def _claim_live(self, c: dict) -> bool:
        if c.get("owner") in _LIVE_OWNERS:
            return True
        if c.get("host") == _HOST:
            if c.get("pid") == os.getpid():
                return False                       # same process, drive loop gone: crashed/aborted driver
            return _pid_alive(c.get("pid", -1)) and self.clock() < float(c.get("lease_expires_at", 0))
        return self.clock() < float(c.get("lease_expires_at", 0))

    def _claim(self, m: Mission, fid: str) -> tuple:
        rt = self.rt
        with _FileLock(rt.store):
            refresh = getattr(rt.store, "refresh", None)
            if refresh:
                refresh()
            st = self.state(m.id, fid)
            if st.claim and self._claim_live(st.claim):
                raise ForkError(f"fork {fid} is being driven by {st.claim.get('owner')}; refusing a second driver")
            gen = st.generation + 1
            owner = f"{_HOST}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
            rt.store.append("ForkDriverClaimed", m.id, {
                "fork_id": fid, "generation": gen, "owner": owner, "host": _HOST, "pid": os.getpid(),
                "lease_expires_at": self.clock() + LEASE_S})
            _LIVE_OWNERS.add(owner)
        return gen, owner

    def _fence(self, m: Mission, fid: str, gen: int, owner: str) -> ForkState:
        """Re-read the log and raise Superseded unless THIS driver (owner, generation) still holds the claim."""
        refresh = getattr(self.rt.store, "refresh", None)
        if refresh:
            refresh()
        st = self.state(m.id, fid)
        if st.generation != gen or (st.claim or {}).get("owner") != owner:
            raise Superseded(f"fork {fid}: driver {owner} (generation {gen}) was superseded by generation "
                             f"{st.generation}; stopping without dispatching or closing")
        return st

    #: test seam: called between the fence check and the append (inside the critical section)
    _between_fence_and_append = None

    def _fenced(self, m: Mission, fid: str, type_: str, payload: dict):
        """Fence + append ATOMICALLY (same cross-process flock + controller lock a takeover must take), so a
        driver superseded at any instant cannot write one more event after the newer claim. Events from a
        fork nobody is driving here (approvals, idle cancel) are plain appends."""
        key = (m.id, fid)
        if key not in self._driving or key not in self._own:
            return self.rt.store.append(type_, m.id, payload)
        with _FileLock(self.rt.store), self._lock:
            self._fence(m, fid, self._gen[key], self._own[key])
            if self._between_fence_and_append is not None:
                self._between_fence_and_append()
            return self.rt.store.append(type_, m.id, payload)

    def _renew(self, m: Mission, fid: str, gen: int, owner: str, state: ForkState) -> None:
        if state.generation != gen or (state.claim or {}).get("owner") != owner:
            raise Superseded(f"fork {fid}: cannot renew, superseded by generation {state.generation}")
        if float(state.claim.get("lease_expires_at", 0)) - self.clock() < LEASE_S / 2:
            self.rt.store.append("ForkDriverClaimed", m.id, {
                "fork_id": fid, "generation": gen, "owner": owner, "host": _HOST, "pid": os.getpid(),
                "lease_expires_at": self.clock() + LEASE_S})

    def _release(self, m: Mission, fid: str, gen: int, owner: str) -> None:
        _LIVE_OWNERS.discard(owner)
        try:
            cur = self.state(m.id, fid)
            if cur.generation != gen or (cur.claim or {}).get("owner") != owner:
                return                              # superseded: never touch the newer driver's claim
            self.rt.store.append("ForkDriverReleased", m.id, {"fork_id": fid, "generation": gen, "owner": owner})
        except BaseException:  # noqa: BLE001 - a dead store (crash) simply leaves the claim to lapse
            pass

    resume_fork = run_fork

    def _tenant(self, m: Mission) -> str:
        t = getattr(m, "tenant", "") or self._tenants.get(m.id)
        if t is None:      # a rehydrated Mission does not carry the tenant; MissionCreated does
            t = self._tenants[m.id] = str(self.rt.repo.created(m.id).get("tenant") or "")
        return t

    @staticmethod
    def _num_ok(v, *, positive=False) -> bool:
        return (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                and (v > 0 if positive else v >= 0))

    def _validate(self, m: Mission, spec: ForkSpec, lim: dict) -> str:
        """Returns the effective failure policy name; raises ForkError for anything invalid."""
        def fid_ok(v):
            return isinstance(v, str) and _ID_RE.fullmatch(v) is not None
        if not fid_ok(spec.fork_id):
            raise ForkError("invalid fork_id")
        if not isinstance(spec.branches, (list, tuple)) or not spec.branches:
            raise ForkError("fork has no branches")
        if not all(isinstance(b, BranchSpec) for b in spec.branches):
            raise ForkError("branches must be BranchSpec")
        ids = [b.branch_id for b in spec.branches]
        if not all(fid_ok(i) for i in ids) or len(set(ids)) != len(ids):
            raise ForkError("branch ids must be unique and match [A-Za-z0-9_.-]{1,64}")
        if len(ids) > lim["max_branches"]:
            raise ForkError(f"{len(ids)} branches exceeds max_branches={lim['max_branches']}")
        j = spec.join
        if not isinstance(j, JoinSpec) or j.mode not in MODES:
            raise ForkError(f"unknown join mode {getattr(j, 'mode', None)!r}")
        if j.mode == "quorum" and not (type(j.k) is int and 1 <= j.k <= len(ids)):
            raise ForkError("quorum requires an int 1 <= k <= number of branches")
        if j.mode != "quorum" and j.k is not None:
            raise ForkError("k is only valid for quorum joins")
        if j.timeout_s is not None and not self._num_ok(j.timeout_s, positive=True):
            raise ForkError("timeout_s must be a finite positive number")
        if j.on_timeout not in ("fail", "partial") or not isinstance(j.cancel_losers, bool):
            raise ForkError("on_timeout must be 'fail' or 'partial' and cancel_losers a bool")
        if spec.max_concurrent is not None and not (type(spec.max_concurrent) is int and spec.max_concurrent > 0):
            raise ForkError("max_concurrent must be a positive int")
        if not isinstance(spec.cancel_siblings_on_branch_cancel, bool):
            raise ForkError("cancel_siblings_on_branch_cancel must be a bool")
        scoped = self.rt._scoped(m)
        for b in spec.branches:
            if not (type(b.max_attempts) is int and 1 <= b.max_attempts <= 10):
                raise ForkError("max_attempts must be an int in 1..10")
            if not isinstance(b.inputs, dict) or not isinstance(b.requires, (tuple, list)):
                raise ForkError("branch inputs must be a dict and requires a list")
            for cap in (b.capability, *b.requires):
                if not isinstance(cap, str) or scoped.get(cap) is None:
                    raise ForkError(f"capability {cap!r} is not granted to mission {m.id}")
            cap = self.rt.registry.get(b.capability)
            c = cap.cost
            if not (self._num_ok(c.usd) and self._num_ok(c.tokens)):
                raise ForkError(f"capability {b.capability!r} declares an invalid cost; refusing to reserve budget")
            if b.produces is not None and b.produces not in (cap.provides or []):
                raise ForkError(f"produces {b.produces!r} is not provided by capability {b.capability!r}")
        mission_policy = _cpol.effective_policy(m, forked=True).value
        want = spec.stricter_failure_policy
        if want is None:
            return mission_policy
        if not isinstance(want, str) or want.upper() not in (mission_policy, _STRICT_OK):
            raise ForkError(f"a fork may only choose {_STRICT_OK} or the mission's policy ({mission_policy}); "
                            f"got {want!r}")
        return want.upper()

    def _open(self, m: Mission, spec: ForkSpec) -> dict:
        rt = self.rt
        with self._lock:
            evs = rt.store.for_mission(m.id)
            existing = fold_fork(evs, spec.fork_id)
            lim = self.limits.for_tenant(self._tenant(m))
            if existing.opened:
                if existing.spec_digest != spec.digest():
                    raise ForkError(f"spec_drift: fork {spec.fork_id} spec differs from the recorded spec_digest")
                policy_eff = existing.failure_policy
            else:
                try:
                    policy_eff = self._validate(m, spec, lim)
                    open_forks = [f for f in fork_ids(evs) if fold_fork(evs, f).closed is None]
                    if len(open_forks) >= lim["max_open_forks"]:
                        raise ForkError(f"max_open_forks={lim['max_open_forks']} reached")
                except (ForkError, TypeError, ValueError, AttributeError) as e:
                    reason = str(e) if isinstance(e, ForkError) else f"invalid spec: {type(e).__name__}"
                    key = _sha([str(getattr(spec, "fork_id", "")), reason])
                    if not any(x.type == "ForkRejected" and x.payload.get("key") == key for x in evs):
                        rt.store.append("ForkRejected", m.id, {"fork_id": str(getattr(spec, "fork_id", ""))[:64],
                                                              "reason": reason, "key": key})
                    raise e if isinstance(e, ForkError) else ForkError(reason)
                conc_req = spec.max_concurrent or lim["max_concurrent_branches"]
                eff = max(1, min(conc_req, lim["max_concurrent_branches"], len(spec.branches)))
                if rt.max_concurrency > 1:
                    eff = min(eff, rt.max_concurrency)
                opened = self.clock()
                dl = None
                if spec.join.timeout_s is not None:
                    dl = opened + float(spec.join.timeout_s)
                if m.deadline is not None:
                    dl = m.deadline if dl is None else min(dl, m.deadline)
                brs = []
                for b in spec.branches:
                    cap = rt.registry.get(b.capability)
                    brs.append({"branch_id": b.branch_id, "node_id": branch_node_id(m.id, spec.fork_id, b.branch_id),
                                "capability": b.capability, "operator": cap.operator if cap else "",
                                "produces": b.produces, "inputs_sha256": _sha(b.inputs),
                                "max_attempts": b.max_attempts})
                rt.store.append("ForkOpened", m.id, {
                    "fork_id": spec.fork_id, "spec_digest": spec.digest(), "branches": brs,
                    "join": vars(spec.join), "failure_policy": policy_eff,
                    "cancel_siblings_on_branch_cancel": spec.cancel_siblings_on_branch_cancel,
                    "limits": {**lim, "effective_concurrency": eff, "tenant": self._tenant(m),
                               "bound_by": ("tenant_limit" if lim["max_concurrent_branches"] <= conc_req
                                            else "fork_request")},
                    "opened_at": opened, "deadline": dl})
        graph = rt._plans[m.id].graph
        if spec.fork_id not in graph.forks:           # single source of "this mission is forked" (#450)
            graph.forks.append(spec.fork_id)
        # branch Node objects (runtime-only; rebuilt identically on every resume)
        nodes: dict[str, Node] = {}
        for b in spec.branches:
            cap = rt.registry.get(b.capability)
            node = Node(capability=b.capability, operator=cap.operator, produces=b.produces,
                        approval_required=cap.approval_required, side_effecting=cap.side_effecting,
                        undo=_comp.declared_undo(cap), inputs={},
                        irreversible=bool(getattr(cap, "irreversible", False)),
                        cost=NodeCost(usd=cap.cost.usd, latency_ms=cap.cost.latency_ms, tokens=cap.cost.tokens,
                                      human_minutes=cap.cost.human_minutes),
                        concurrency_mode=getattr(cap, "concurrency_mode", "") or "",
                        concurrency_key=getattr(cap, "concurrency_key", "") or "",
                        resource_keys=list(getattr(cap, "resource_keys", []) or []),
                        max_parallelism=getattr(cap, "max_parallelism", None))
            node.id = branch_node_id(m.id, spec.fork_id, b.branch_id)
            nodes[b.branch_id] = node
        return nodes

    # ── inputs (branch-scoped; never mission inputs, never approval keys) ──
    def _inputs(self, m: Mission, spec_b: BranchSpec, node: Node, bs: BranchState) -> dict:
        rt = self.rt
        resolved: dict[str, Any] = {"_goal": m.goal, "_mission": str(m.id)}
        world = rt._world(m.id)
        for k, v in strip_approval_keys(dict(spec_b.inputs)).items():
            if k in _RESERVED_INPUT_KEYS:
                continue
            if isinstance(v, dict) and "$from_world" in v:
                resolved[k] = world.get(v["$from_world"])
            else:
                resolved[k] = copy.deepcopy(v)
        if bs.approval is not None and bs.approval == node.capability:
            marker: dict = {"approved": True}
            edit = None
            for e in rt.store.for_mission(m.id):
                if e.type == "ApprovalGranted" and e.payload.get("node_id") == node.id:
                    edit = e.payload.get("edit")
            if isinstance(edit, dict):
                marker.update({k: v for k, v in edit.items() if not is_approval_key(k)})
            resolved["_approval"] = marker
            resolved["_node"] = node.id
        return resolved

    # ── budget ──
    def _spent(self, m: Mission) -> tuple:
        usd = tok = 0.0
        rt = self.rt
        status = rt.repo.node_status(m.id)
        for n in rt._plans[m.id].graph.nodes:
            if status.get(n.id) == NodeState.DONE:
                usd += n.cost.usd
                tok += n.cost.tokens
        for e in rt.store.for_mission(m.id):
            if e.type == "BranchStarted":
                r = e.payload.get("reserved") or {}
                usd += float(r.get("usd", 0.0))
                tok += float(r.get("tokens", 0))
        return usd, tok

    # ── drive ──
    def _drive(self, m: Mission, spec: ForkSpec, nodes: dict, gen: int, owner: str) -> ForkResult:
        rt = self.rt
        fid = spec.fork_id
        key = (m.id, fid)
        by_id = {b.branch_id: b for b in spec.branches}
        with self._lock:
            self._driving.add(key)
            self._gen[key] = gen
            self._own[key] = owner
        futs: dict[str, _cf.Future] = {}
        try:
            if m.state != MissionState.RUNNING:
                rt._set_state(m, MissionState.RUNNING)
            self._settle_confirmed(m, self.state(m.id, fid))
            while True:
                st = self.state(m.id, fid)
                if st.closed:
                    self._apply_close(m, st)
                    return ForkResult(st.closed, st)
                self._fence(m, fid, gen, owner)
                self._renew(m, fid, gen, owner, self.state(m.id, fid))
                self._harvest(m, st, spec, by_id, nodes, futs)
                st = self.state(m.id, fid)
                # forced cancellation: mission cancel / deadline
                forced = self._forced_reason(m, st)
                if forced:
                    self._cancel_nonterminal(m, st, forced, futs, nodes)
                    st = self.state(m.id, fid)
                verdict = self._verdict(st)
                partial = False
                if (forced == "join_timeout" and verdict == "failed" and st.join.get("on_timeout") == "partial"
                        and st.counts()["succeeded"] > 0):
                    verdict, partial = "satisfied", True
                if verdict == "satisfied":
                    if not forced:
                        if st.join.get("cancel_losers", True):
                            self._cancel_nonterminal(m, st, "join_satisfied", futs, nodes)
                        else:
                            self._cancel_unstarted(m, st, "join_satisfied")
                            st = self.state(m.id, fid)
                            if self._start_ready(m, st, spec, by_id, nodes, futs):   # crash-orphaned in-flight
                                continue
                            if futs:               # drain: keep in-flight work, then close
                                self._wait(futs)
                                continue
                        st = self.state(m.id, fid)
                    self._finalize(m, st, "satisfied", partial=partial, reason=forced or "join_satisfied")
                    continue
                if verdict == "failed":
                    self._cancel_unstarted(m, st, forced or "join_failed")
                    st = self.state(m.id, fid)
                    # a branch that was in flight when a previous process died is re-dispatched with its SAME
                    # idempotency key so its outcome is recorded (drained) rather than left unknown
                    if not forced and self._start_ready(m, st, spec, by_id, nodes, futs):
                        continue
                    if futs and not forced:        # drain: record in-flight siblings' work before JoinFailed
                        self._wait(futs)
                        continue
                    st = self.state(m.id, fid)
                    if not forced and any(b.awaiting and b.status == "reconciling" for b in st.ordered()):
                        # The join can no longer succeed, but what it must compensate depends on a call that may have
                        # applied its effect. Deciding now would roll back around an unknown: wait for the evidence.
                        rt._set_state(m, MissionState.WAITING_RECONCILIATION)
                        return ForkResult("awaiting_reconciliation", st)
                    self._finalize(m, st, "failed", reason=forced or self._failed_reason(st))
                    continue
                started = self._start_ready(m, st, spec, by_id, nodes, futs)
                st = self.state(m.id, fid)
                if started:
                    continue
                if not futs:
                    if any(b.status == "parked" for b in st.ordered()):
                        rt._set_state(m, MissionState.WAITING_HUMAN)
                        return ForkResult("parked", st)
                    if any(b.status in ("pending", "retrying", "running") for b in st.ordered()):
                        if self._slots_blocked(m):
                            time.sleep(self.poll_s)
                            continue
                        for b in st.ordered():       # unschedulable: fail closed, never spin
                            if b.status in ("pending", "retrying", "running"):
                                self._append_failed(m, st, b, b.attempt or 1, "unschedulable", ran=False)
                        continue
                    if any(b.status == "reconciling" for b in st.ordered()):
                        # Everything that can run has run. What is left is a call whose outcome is unknown: that is a wait on
                        # evidence (a probe or a person), not a failure, and it is not resent. The mission parks; the same
                        # fork resumes when the branch is reconciled.
                        rt._set_state(m, MissionState.WAITING_RECONCILIATION)
                        return ForkResult("awaiting_reconciliation", st)
                self._wait(futs)
        finally:
            with self._lock:
                self._driving.discard(key)

    def _slots_blocked(self, m: Mission) -> bool:
        lim = self.limits.for_tenant(self._tenant(m))["max_concurrent_branches"]
        return self._tenant_slots.get(self._tenant(m), 0) >= lim

    def _wait(self, futs: dict) -> None:
        if futs:
            _cf.wait(list(futs.values()), timeout=self.poll_s, return_when=_cf.FIRST_COMPLETED)
        else:
            time.sleep(self.poll_s)

    def _verdict(self, st: ForkState) -> str:
        c = st.counts()
        return evaluate_join(st.join.get("mode", "all"), st.join.get("k"), c["n"],
                             c["succeeded"], c["failed"], c["cancelled"])

    @staticmethod
    def _failed_reason(st: ForkState) -> str:
        bad = [b for b in st.ordered() if b.status in ("failed", "cancelled")]
        return f"{st.join.get('mode')}_join_unsatisfiable: " + ", ".join(
            f"{b.branch_id}={b.reason or b.status}" for b in bad)

    def _forced_reason(self, m: Mission, st: ForkState) -> str:
        if m.id in self._cancelled or self.rt._missions[m.id].state == MissionState.CANCELLED:
            return "mission_cancelled"
        if st.deadline is not None and self.clock() >= float(st.deadline):
            tmo = st.join.get("timeout_s")
            if m.deadline is not None and (tmo is None or float(st.deadline) >= m.deadline):
                return "mission_deadline"
            return "join_timeout"
        return ""

    # ── scheduling ──
    def _start_ready(self, m, st: ForkState, spec: ForkSpec, by_id, nodes, futs) -> bool:
        rt = self.rt
        lim = self.limits.for_tenant(self._tenant(m))
        cap_fork = int(st.limits.get("effective_concurrency") or 1)
        started = False
        inflight: dict[str, int] = {}
        ops: dict[str, int] = {}
        for bid in futs:
            conc.acquire(nodes[bid], inflight)
            ops[nodes[bid].operator] = ops.get(nodes[bid].operator, 0) + 1
        plan = rt._plans[m.id]
        world = rt._world(m.id)
        for bs in st.ordered():                      # plan order: deterministic
            gen = self._gen.get((m.id, st.fork_id), 1)
            # a 'running' branch not in OUR futures is re-dispatched only when it was started by an OLDER driver
            # generation (a detected restart); never from a concurrent driver of the same generation.
            if bs.status not in ("pending", "retrying") and not (
                    bs.status == "running" and bs.branch_id not in futs and bs.generation < gen):
                continue
            node = nodes[bs.branch_id]
            if len(futs) >= cap_fork:
                break
            if conc.conflict(node, inflight) is not None:
                continue
            lim_op = rt.policy.per_operator_limit.get(node.operator)
            if lim_op is not None and ops.get(node.operator, 0) >= lim_op:
                continue
            attempt = (bs.attempt + 1) if bs.status == "retrying" else max(bs.attempt, 1)
            resumed = bs.status == "running"
            regate = False
            if resumed:
                # a branch an older driver had in flight: what its reservation says decides whether it may be sent again
                handled, regate = self._resume_lease(m, st, bs, node, attempt)
                if handled:
                    started = True
                    continue
            # scope: capability must still be granted (policy tightened since open)
            if rt._scoped(m).get(node.capability) is None:
                self._append_failed(m, st, bs, attempt, "policy_revoked", ran=False)
                started = True
                continue
            if bs.approval is not None and bs.approval != node.capability:
                bs.approval = None                    # approval bound to a different capability: void
            if bs.approval is None:
                decision = rt.context.resolve(ContextIntent(kind=CHECK_POLICY, node=node, world=world,
                                                            mission=m, graph=plan.graph)).value
                if getattr(decision, "policy_digest", ""):
                    rt.store.append("PolicyEvaluated", m.id, {
                        "node_id": node.id, "capability": node.capability, "policy": decision.policy_ref,
                        "policy_digest": decision.policy_digest,
                        "effect": decision.explain.get("effect", "allow"),
                        "matched_rule": decision.explain.get("matched_rule", "")})
                if getattr(decision, "denied", False):
                    self._append_failed(m, st, bs, attempt, "policy_denied", ran=False)
                    started = True
                    continue
                # one decision for every execution topology: DENY -> mandatory -> standing grant -> ordinary approval
                verdict, ares = ("run", None) if resumed and not regate else rt._authority_gate(
                    m, node, decision, world, graph=plan.graph, inputs=self._inputs(m, by_id[bs.branch_id], node, bs),
                    attempt=attempt)
                if verdict == "authority_denied":
                    self._append_failed(m, st, bs, attempt, "authority_denied", ran=False,
                                        error=getattr(ares, "reason", "") or "authority_denied")
                    started = True
                    continue
                if verdict == "park":
                    self._park(m, node, decision, st.fork_id, bs.branch_id)
                    started = True
                    continue
            if not rt._pre_exec_ok(m, plan, node):
                self._append_failed(m, st, bs, attempt, "admission_refused", ran=False)
                started = True
                continue
            if not self._acquire_slot(m, lim):
                break
            submitted = False
            try:                                      # the slot is released on EVERY non-submitting exit
                b_spec = by_id[bs.branch_id]
                idem = branch_idempotency_key(m.id, st.fork_id, bs.branch_id, attempt)
                with self._lock:                      # atomic: budget check + reservation + durable start
                    reserved = {} if resumed else {"usd": node.cost.usd, "tokens": node.cost.tokens}
                    if not resumed:
                        usd, tok = self._spent(m)
                        # `not (x <= limit)` also refuses NaN; costs were validated finite and >= 0
                        ok = (usd + node.cost.usd <= m.budget.usd + 1e-9
                              and (not node.cost.tokens or tok + node.cost.tokens <= m.budget.tokens))
                        if not ok:
                            self._append_failed(m, st, bs, attempt, "budget_exceeded", ran=False)
                            started = True
                            continue
                    self._fence(m, st.fork_id, gen, self._own[(m.id, st.fork_id)])   # no dispatch when stale
                    self._fenced(m, st.fork_id, "BranchStarted", {
                        "fork_id": st.fork_id, "branch_id": bs.branch_id, "node_id": node.id,
                        "attempt": attempt, "idempotency_key": idem, "reserved": reserved,
                        "resumed": resumed, "generation": gen})
                node.idempotency_key = idem
                inputs = self._inputs(m, b_spec, node, bs)
                claims = rt._lease_dispatched(m, node)  # BranchStarted is durable: the call may now leave
                if claims is None and rt._lease_of(m, node.id) is not None:
                    # the reservation was already dispatched by an earlier attempt: do not send it again (see runtime._refuse_redispatch)
                    self._park_unknown(m, st, bs, node, attempt,
                                       "already dispatched by an earlier attempt; the outcome is unknown")
                    started = True
                    continue
                if claims:
                    inputs = {**inputs, "_exec_auth": claims}
                fut = self._submit(m, node, inputs)
                submitted = True
                fut.add_done_callback(lambda _f, mm=m: self._release_slot(mm))
            finally:
                if not submitted:
                    self._release_slot(m)
            futs[bs.branch_id] = fut
            conc.acquire(node, inflight)
            ops[node.operator] = ops.get(node.operator, 0) + 1
            started = True
        return started

    # ── unknown outcomes: the same reconciliation contract as a plan node ──
    def _park_unknown(self, m: Mission, st: ForkState, bs: BranchState, node: Node, attempt: int, reason: str,
                      lease_id: str = "") -> None:
        """Record that this branch's dispatched call has an unknown outcome. The record is the SAME `ReconciliationRequired` a
        plan node writes, so the inbox, the supervisor's probe jobs, the watchdog classification and `reconcile_node` see a fork
        branch exactly like any other parked node. The branch is neither failed (the effect may exist) nor retried (that could
        repeat it); it holds until evidence arrives. Idempotent."""
        rt = self.rt
        if self.state(m.id, st.fork_id).branches[bs.branch_id].awaiting:
            return
        ent = rt._lease_of(m, node.id)
        lid = lease_id or (ent[0] if ent else "")
        if lid and rt.authority is not None:
            lease = rt.authority.lease(lid)
            if lease is not None and lease["state"] == "dispatched":
                rt.authority.unknown(lid)
                rt.store.append("AuthorityLeaseUnknown", m.id, {"node_id": node.id, "lease_id": lid})
        with self._lock:
            self._fenced(m, st.fork_id, "ReconciliationRequired", {
                "fork_id": st.fork_id, "branch_id": bs.branch_id, "node_id": node.id, "capability": node.capability,
                "operator": getattr(node, "operator", ""), "lease_id": lid, "attempt": attempt,
                "idempotency_key": bs.idempotency_key or getattr(node, "idempotency_key", "") or "",
                "reason": reason[:300], "at": time.time()})
        rt.lifecycle.dispatch(GateReached(mission_id=m.id, node_id=node.id, capability=node.capability))

    def _resume_lease(self, m: Mission, st: ForkState, bs: BranchState, node: Node, attempt: int) -> tuple:
        """A branch whose BranchStarted is durable but whose driver is gone. Returns (handled, regate). Its reservation
        (`<mission>:<node>:a<attempt>`, derived, so it needs no memory of the old process) says what may happen:

          reserved      never dispatched: the call did not leave; send it
          dispatched /  it may have executed. A read-only call is simply run again (its reservation is returned and re-taken);
          unknown       a side-effecting call is NEVER sent again: it waits for reconciliation
          settled       the effect is confirmed but this branch recorded no result: it waits for reconciliation (a result is needed)
          released      it did not execute: re-attempt if the branch has attempts left, else it fails cleanly
        """
        rt = self.rt
        g = rt.authority
        if g is None:
            return False, False
        lid = f"{m.id}:{node.id}:a{attempt}"
        lease = g.lease(lid)
        if lease is None:
            return False, False
        state = lease["state"]
        if state == "reserved":
            rt._leases()[(str(m.id), node.id)] = (lid, bool(node.side_effecting),
                                                  (lease.get("meta") or {}).get("envelope_digest", ""), attempt)
            return False, False
        if state in ("dispatched", "unknown"):
            if not node.side_effecting:
                if g.reconcile(lid, executed=False):            # nothing external to repeat: take the reservation again
                    rt._leases().pop((str(m.id), node.id), None)
                    return False, True
            self._park_unknown(m, st, bs, node, attempt, "the driver was lost with this call in flight; the outcome is unknown",
                               lease_id=lid)
            return True, False
        if state == "settled":
            self._park_unknown(m, st, bs, node, attempt, "the effect is confirmed but no result was recorded", lease_id=lid)
            return True, False
        if state == "released":
            self._append_failed(m, st, bs, attempt, "lease_released", ran=False,
                                will_retry=attempt < self._max_attempts(m.id, st.fork_id, bs))
            return True, False
        return False, False

    def _settle_confirmed(self, m: Mission, st: ForkState) -> None:
        """A branch the log records as succeeded whose reservation is still open (the driver died between recording the success and
        settling the ledger): the log is the proof, so settle it. Otherwise the counters would stay held for a finished call."""
        g = self.rt.authority
        if g is None:
            return
        for b in st.ordered():
            if b.status == "succeeded" and b.attempt and not b.reason:
                lid = f"{m.id}:{b.node_id}:a{b.attempt}"
                lease = g.lease(lid)
                if lease is not None and lease["state"] in ("dispatched", "unknown"):
                    g.settle(lid)

    def _max_attempts(self, mission_id: str, fork_id: str, bs: BranchState) -> int:
        spec = self._specs.get((mission_id, fork_id))
        return next((x.max_attempts for x in (spec.branches if spec else []) if x.branch_id == bs.branch_id), bs.max_attempts)

    def reconcile_branch(self, m: Mission, mission_id: str, node_id: str, *, executed: bool, evidence: dict, actor: str,
                         reason: str, result: "dict | None" = None, resume: bool = True) -> Mission:
        """`MissionRuntime.reconcile_node` for a fork branch. Same rules as a plan node (validated first; the ledger's atomic
        transition decides, the first resolver wins; repeating the same decision converges), and the same fork continues:
        branches that succeeded are not rerun, branches still waiting stay waiting, the fork is driven again from its log."""
        from .reconcile import ReconciliationConflict, ReconciliationError, validate_evidence  # noqa: PLC0415
        rt = self.rt
        ev = validate_evidence(evidence)
        evs = rt.store.for_mission(mission_id)
        target = None
        for fid in fork_ids(evs):
            fs = fold_fork(evs, fid)
            for b in fs.ordered():
                if b.node_id == node_id:
                    target = (fs, b)
        if target is None:
            raise ReconciliationError("this node is not a fork branch of the mission")
        st, b = target
        rec = next((p for p in rt.repo.pending_reconciliation(mission_id) if p["node_id"] == node_id), None)
        if rec is None:
            prior = rt.repo.last_reconciliation(mission_id, node_id)
            if prior is None:
                raise ReconciliationError("this node has no unresolved reconciliation")
            if bool(prior.get("executed")) != bool(executed):
                raise ReconciliationConflict(f"this node was already reconciled as "
                                             f"{'executed' if prior.get('executed') else 'not executed'} by {prior.get('actor')}")
            return self._after_reconcile(m, mission_id, st.fork_id, resume)
        if executed and b.produces and not result:
            raise ReconciliationError(f"branch {b.branch_id} produces '{b.produces}': supply the provider's real result so the "
                                      "fork's join publishes facts, not a placeholder")
        lid = rec.get("lease_id") or ""
        lease = rt.authority.lease(lid) if lid else None
        if lease is None:
            raise ReconciliationError("the reservation behind this branch is not in the authority ledger; resolve it there first")
        want = "settled" if executed else "released"
        if lease["state"] in ("dispatched", "unknown"):
            if not rt.authority.reconcile(lid, executed=executed):
                lease = rt.authority.lease(lid)
                if lease is None or lease["state"] != want:
                    raise ReconciliationConflict(f"the reservation was resolved the other way ({lease['state'] if lease else 'gone'})")
        elif lease["state"] != want:
            raise ReconciliationConflict(f"the reservation is already {lease['state']}; it cannot be reconciled as "
                                         f"{'executed' if executed else 'not executed'}")
        rt.authority.annotate(lid, reconcile_status="resolved_manually", resolved_by=str(actor).strip(),
                              resolution_reason=str(reason).strip()[:500], evidence=ev, resolved_at=time.time(),
                              manual_outcome="executed" if executed else "not_executed",
                              provider_ref=ev.get("provider_ref") or None)
        attempt = int(rec.get("attempt") or b.attempt or 1)
        max_attempts = self._max_attempts(mission_id, st.fork_id, b)
        with self._lock:
            if executed:
                out = {**(dict(result) if isinstance(result, dict) else {}), "reconciled": True,
                       "reconciled_by": str(actor).strip(), "evidence": ev}
                rcpt = {"fork_id": st.fork_id, "branch_id": b.branch_id, "node_id": node_id, "capability": b.capability,
                        "operator": b.operator, "attempt": attempt, "idempotency_key": rec.get("idempotency_key", ""),
                        "result_sha256": _sha(out), "reconciled": True}
                # a branch the fork already gave up on (cancelled, or the join closed) keeps the receipt as a LATE one
                late = b.status in _TERMINAL_BRANCH or st.closed is not None
                if b.status != "succeeded":
                    self._fenced(m, st.fork_id, "BranchSucceeded", {
                        "fork_id": st.fork_id, "branch_id": b.branch_id, "node_id": node_id, "attempt": attempt,
                        "result": out, "receipt": rcpt, **({"late": True} if late else {})})
            elif b.status == "reconciling":
                # authoritative negative: the call did not happen, so a retry is a NEW attempt under a fresh reservation
                self._fenced(m, st.fork_id, "BranchFailed", {
                    "fork_id": st.fork_id, "branch_id": b.branch_id, "node_id": node_id, "attempt": attempt,
                    "reason": "reconciled_not_executed", "error": "", "ran": False,
                    "will_retry": attempt < max_attempts})
            rt.store.append("ReconciliationResolved", mission_id, {
                "node_id": node_id, "lease_id": lid, "executed": bool(executed), "actor": str(actor).strip(),
                "reason": str(reason).strip()[:500], "evidence": ev, "provider_ref": ev.get("provider_ref", ""),
                "fork_id": st.fork_id, "branch_id": b.branch_id, "at": time.time()})
        rt._leases().pop((str(m.id), node_id), None)
        return self._after_reconcile(m, mission_id, st.fork_id, resume)

    def _after_reconcile(self, m: Mission, mission_id: str, fork_id: str, resume: bool) -> Mission:
        """Continue the SAME fork from its log if its spec is known to this process; otherwise leave the mission paused for the
        owner to `register_spec` + `run_fork` (the spec's inputs are not in the log)."""
        terminal = (MissionState.SUCCEEDED, MissionState.FAILED, MissionState.CANCELLED, MissionState.COMPENSATION_FAILED)
        if m.state in terminal or self.state(mission_id, fork_id).closed:
            return m
        spec = self._specs.get((mission_id, fork_id))
        if not resume or spec is None or (mission_id, fork_id) in self._driving:
            if m.state is MissionState.WAITING_RECONCILIATION and not self.rt.repo.pending_reconciliation(mission_id):
                self.rt._set_state(m, MissionState.PAUSED)
            return m
        self.run_fork(mission_id, spec)
        return m

    def _submit(self, m: Mission, node: Node, inputs: dict) -> _cf.Future:
        fut: _cf.Future = _cf.Future()
        ex = self.rt._executor_for(m)

        def _run():
            try:
                fut.set_result(ex.run(node, inputs))
            except BaseException as e:  # noqa: BLE001 - delivered through the future
                fut.set_exception(e)
        t = threading.Thread(target=_run, name=f"branch-{node.id}", daemon=True)
        t.start()
        return fut

    def _acquire_slot(self, m: Mission, lim: dict) -> bool:
        t = self._tenant(m)
        with self._lock:
            if self._tenant_slots.get(t, 0) >= lim["max_concurrent_branches"]:
                return False
            self._tenant_slots[t] = self._tenant_slots.get(t, 0) + 1
            return True

    def _release_slot(self, m: Mission) -> None:
        t = self._tenant(m)
        with self._lock:
            self._tenant_slots[t] = max(0, self._tenant_slots.get(t, 0) - 1)

    # ── parking (approvals flow through the mission's approve path) ──
    def _park(self, m: Mission, node: Node, decision, fork_id: str, branch_id: str) -> None:
        rt = self.rt
        node.status = NodeState.WAITING
        rules = decision.rules if decision else []
        why = f" [{', '.join(rules)}]" if rules else ""
        task = HumanTask(mission_id=m.id, node_id=node.id, assignee=rt._assignee(node),
                         prompt=f"Approve '{node.capability}' ({node.operator}) for branch "
                                f"{branch_id} of fork {fork_id}?{why}",
                         evidence=[f"capability={node.capability}", f"side_effecting={node.side_effecting}",
                                   f"fork={fork_id}", f"branch={branch_id}"])
        from .types import to_jsonable  # noqa: PLC0415
        rt.store.append("NodeParked", m.id, {
            "node_id": node.id, "capability": node.capability, "rules": rules,
            "risk": to_jsonable(decision.risk) if decision else {},
            "approval_packet": decision.packet if decision else {},
            "fork_id": fork_id, "branch_id": branch_id,
            "human_task": {"id": task.id, "assignee": task.assignee, "prompt": task.prompt,
                           "evidence": task.evidence, "options": task.options}})
        rt.lifecycle.dispatch(GateReached(mission_id=m.id, node_id=node.id, capability=node.capability))
        rt._set_state(m, MissionState.WAITING_HUMAN)

    def approve_branch(self, mission_id: str, node_id: str, decision: str = "approve",
                       edit: dict | None = None) -> Mission:
        """Called by ``MissionRuntime.approve`` for a branch node id. Only a currently parked branch can be
        approved/rejected (no pre-approval). The approval is bound to the branch's capability."""
        rt = self.rt
        if decision not in ("approve", "reject"):
            raise ValueError(f"a fork branch accepts only 'approve' or 'reject', not {decision!r}")
        from .runtime import ApprovalError  # noqa: PLC0415
        if edit is not None and not isinstance(edit, dict):
            raise ValueError("edit must be a dict or None")
        m = rt._missions[mission_id]
        if m.state in (MissionState.SUCCEEDED, MissionState.FAILED, MissionState.COMPENSATION_FAILED,
                       MissionState.CANCELLED):
            raise ApprovalError(f"mission is {m.state.value}; nothing to approve")
        evs = rt.store.for_mission(mission_id)
        target = None
        for fid in fork_ids(evs):
            st = fold_fork(evs, fid)
            for b in st.ordered():
                if b.node_id == node_id:
                    target = (st, b)
        if target is None:
            raise ApprovalError(f"node {node_id!r} is not a fork branch of this mission")
        if target[0].closed is not None:
            raise ApprovalError(f"fork {target[0].fork_id} is already {target[0].closed}; nothing to approve")
        if not target[1].parked:
            raise ApprovalError(f"branch {node_id!r} has no pending approval")
        st, b = target
        with self._lock:
            if decision == "approve":
                rt.store.append("ApprovalGranted", mission_id, {
                    "node_id": node_id, "edit": edit, "capability": b.capability,
                    "fork_id": st.fork_id, "branch_id": b.branch_id})
                rt.store.append("NodeResumed", mission_id, {"node_id": node_id})
            else:
                rt.store.append("ApprovalRejected", mission_id, {"node_id": node_id})
                rt.store.append("NodeSkipped", mission_id, {"node_id": node_id})
                self._append_failed(m, st, b, max(b.attempt, 1), "approval_rejected", ran=False)
        spec = self._specs.get((mission_id, st.fork_id))
        if spec is not None and (mission_id, st.fork_id) not in self._driving:
            self.run_fork(mission_id, spec)
        return m

    # ── commit (persist BEFORE join) ──
    def _harvest(self, m, st, spec, by_id, nodes, futs) -> None:
        for bid in list(futs):
            fut = futs[bid]
            if not fut.done():
                continue
            del futs[bid]
            self._commit(m, st, by_id[bid], nodes[bid], fut)
            st = self.state(m.id, st.fork_id)

    def _commit(self, m, st, b_spec: BranchSpec, node: Node, fut: _cf.Future) -> None:
        rt = self.rt
        bs = st.branches[b_spec.branch_id]
        attempt = bs.attempt or 1
        exc = fut.exception()
        if isinstance(exc, SimulatedCrash):
            raise exc
        if exc is not None:
            rt._meter(m, node, "error")
            rt.learning.record_capability(node.capability, False)
            # A retry is a NEW idempotency key. After a real call failed, a side-effecting capability might have
            # applied the effect, so it is retried only if the failure explicitly says nothing was applied.
            safe = (not node.side_effecting) or bool(getattr(exc, "not_applied", False))
            if not safe and rt.authority is not None and rt._lease_of(m, node.id) is not None:
                # It may have applied its effect and nothing says it did not: the same contract as a plan node. The reservation
                # stays counted (UNKNOWN), the branch waits for evidence, and nothing is sent again.
                rt._lease_failed(m, node.id, ran=True, executed=None)
                self._park_unknown(m, st, bs, node, attempt, f"the call failed after it may have been sent: {str(exc)[:200]}")
                return
            self._append_failed(m, st, bs, attempt, "operator_error", ran=True, error=str(exc),
                                will_retry=attempt < b_spec.max_attempts and safe,
                                not_applied=bool(getattr(exc, "not_applied", False)))
            return
        result = fut.result()
        inputs_hash = _sha({k: v for k, v in b_spec.inputs.items() if not is_approval_key(k)})
        approved = bs.approval is not None and bs.approval == node.capability
        receipt = {"fork_id": st.fork_id, "branch_id": bs.branch_id, "node_id": node.id,
                   "capability": node.capability, "operator": node.operator, "attempt": attempt,
                   "idempotency_key": bs.idempotency_key, "input_sha256": inputs_hash,
                   "result_sha256": _sha(result), "approved": approved}
        if is_staged(result) and not approved:
            rt._meter(m, node, "error")
            rt.learning.record_capability(node.capability, False)
            self._append_failed(m, st, bs, attempt, "staged_not_executed", ran=True, receipt=receipt,
                                result=result)
            return
        if needs_verification(node):
            world = rt._world(m.id)
            vr = rt.verifier.verify(node, result, world, m)
            rt.evidence.record_verification(m.id, node.id, vr)
            rt.learning.record_verification(node.capability, vr.decision == VerificationDecision.ACCEPT)
            if vr.decision != VerificationDecision.ACCEPT:
                rt._meter(m, node, "error")
                rt.learning.record_capability(node.capability, False)
                self._append_failed(m, st, bs, attempt, f"verification_{vr.decision.value}", ran=True,
                                    receipt=receipt, result=result)
                return
        with self._lock:
            self._fenced(m, st.fork_id, "BranchSucceeded", {
                "fork_id": st.fork_id, "branch_id": bs.branch_id, "node_id": node.id, "attempt": attempt,
                "result": result, "receipt": receipt})
        rt._lease_settle(m, node.id, result)
        rt._meter(m, node, "ok")
        rt.learning.record_capability(node.capability, True)

    def _append_failed(self, m, st, bs, attempt, reason, *, ran, error="", will_retry=False,
                       receipt=None, result=None, not_applied=False) -> None:
        payload = {"fork_id": st.fork_id, "branch_id": bs.branch_id, "node_id": bs.node_id,
                   "attempt": attempt, "reason": reason, "error": error, "ran": ran,
                   "will_retry": bool(will_retry)}
        if receipt is not None:
            payload["receipt"] = receipt
        if result is not None:
            payload["result"] = result
        with self._lock:
            self._fenced(m, st.fork_id, "BranchFailed", payload)
        # reservation lifecycle: never dispatched -> release; staged -> did not execute; verification -> it executed;
        # an operator error is ambiguous for a side-effecting capability (kept UNKNOWN) unless it says not_applied
        self.rt._lease_failed(m, bs.node_id, ran=ran, executed=(
            False if reason == "staged_not_executed" else True if reason.startswith("verification_") else
            False if not_applied else None))

    # ── cancellation ──
    def cancel_branch(self, mission_id: str, fork_id: str, branch_id: str, reason: str = "cancelled") -> None:
        """Cancel ONE branch. Siblings are cancelled only if the fork's join policy says so
        (``cancel_siblings_on_branch_cancel``). A running call cannot be killed: if it completes later its
        result is kept as a late receipt, never silently dropped."""
        m = self.rt._missions[mission_id]
        with self._lock:
            st = self.state(mission_id, fork_id)
            b = st.branches.get(branch_id)
            if b is None or st.closed or b.status in _TERMINAL_BRANCH:
                return
            self._append_cancel(m, st, b, reason)
            if st.csbc:
                st = self.state(mission_id, fork_id)
                for o in st.ordered():
                    if o.status not in _TERMINAL_BRANCH:
                        self._append_cancel(m, st, o, f"sibling_cancelled:{branch_id}")

    def _append_cancel(self, m, st, b: BranchState, reason: str) -> None:
        if b.parked:                                  # close the open human gate so needs-you is not stale
            self.rt.store.append("NodeSkipped", m.id, {"node_id": b.node_id, "reason": reason})
        self._fenced(m, st.fork_id, "BranchCancelled", {
            "fork_id": st.fork_id, "branch_id": b.branch_id, "node_id": b.node_id, "reason": reason,
            "was_running": b.status == "running"})

    def _cancel_unstarted(self, m, st, reason: str) -> None:
        with self._lock:
            for b in st.ordered():
                if b.status in ("pending", "retrying", "parked"):
                    self._append_cancel(m, st, b, reason)

    def _cancel_nonterminal(self, m, st, reason: str, futs: dict, nodes: dict) -> None:
        with self._lock:
            for b in st.ordered():
                if b.status in _TERMINAL_BRANCH:
                    continue
                self._append_cancel(m, st, b, reason)
                fut = futs.pop(b.branch_id, None)
                if fut is not None:
                    fut.add_done_callback(
                        lambda f, mm=m, ss=st, bb=b, nd=nodes[b.branch_id]: self._late(mm, ss, bb, nd, f))

    def _late(self, m, st, bs: BranchState, node: Node, fut: _cf.Future) -> None:
        """A cancelled branch's call finished anyway: keep its receipt visible (late), change nothing else."""
        try:
            if fut.exception() is not None:
                return
            result = fut.result()
            receipt = {"fork_id": st.fork_id, "branch_id": bs.branch_id, "node_id": node.id,
                       "capability": node.capability, "operator": node.operator,
                       "attempt": bs.attempt or 1, "idempotency_key": bs.idempotency_key,
                       "result_sha256": _sha(result), "late": True}
            with self._lock:
                self.rt.store.append("BranchSucceeded", m.id, {
                    "fork_id": st.fork_id, "branch_id": bs.branch_id, "node_id": node.id,
                    "attempt": bs.attempt or 1, "result": result, "receipt": receipt, "late": True})
            self.rt._lease_settle(m, node.id)
        except BaseException:  # noqa: BLE001 - a late callback must never raise (nor kill the worker)
            pass

    # ── close ──
    def _finalize(self, m: Mission, st: ForkState, kind: str, *, reason: str, partial: bool = False) -> None:
        rt = self.rt
        c = st.counts()
        brief = lambda ids: [self._brief(st.branches[i]) for i in ids]  # noqa: E731
        with self._lock:
            key = (m.id, st.fork_id)
            self._fence(m, st.fork_id, self._gen[key], self._own[key])   # a stale driver never closes a fork
            if self.state(m.id, st.fork_id).closed:
                return
            if kind == "satisfied":
                self._fenced(m, st.fork_id, "JoinSatisfied", {
                    "fork_id": st.fork_id, "mode": st.join.get("mode"), "k": st.join.get("k"),
                    "winners": _winners(st), "failed": st.ids("failed"), "cancelled": st.ids("cancelled"),
                    "abandoned": [], "partial": partial, "reason": reason})
            else:
                ctx = FailureContext(
                    mission_id=m.id, fork_id=st.fork_id, join_mode=st.join.get("mode", "all"), reason=reason,
                    succeeded=brief(st.ids("succeeded")), failed=brief(st.ids("failed")),
                    cancelled=brief(st.ids("cancelled")), tenant=self._tenant(m),
                    policy=st.failure_policy or "HALT_AND_REPORT")
                decision = self._decide(m, st, ctx)
                self._fenced(m, st.fork_id, "JoinFailed", {
                    "fork_id": st.fork_id, "mode": st.join.get("mode"), "k": st.join.get("k"),
                    "reason": reason, "succeeded": st.ids("succeeded"), "failed": st.ids("failed"),
                    "cancelled": st.ids("cancelled"), "failure_policy": ctx.policy,
                    "decision": decision.to_dict()})

    def _caps_of(self, b: BranchState):
        return self.rt.registry.get(b.capability)

    def _decide(self, m: Mission, st: ForkState, ctx: FailureContext) -> FailureDecision:
        """Map the MISSION's failure policy (compensation_policy.decide_on_branch_failure, #450) to a
        FailureDecision. Pure; the fork never decides on its own."""
        def view(b: BranchState, nodes: bool) -> dict:
            cap = self._caps_of(b)
            node = {"id": b.node_id, "capability": b.capability, "status": "done",
                    "side_effecting": bool(getattr(cap, "side_effecting", False)),
                    "undo": _comp.declared_undo(cap) if cap is not None else None,
                    "irreversible": bool(getattr(cap, "irreversible", False)),
                    "disposition": "PRESERVE" if (cap is not None and _comp.is_preserved(cap))
                    else "CLEANUP" if (cap is not None and _comp.is_cleanup(cap)) else ""}
            return {"id": b.branch_id, "status": b.status, "nodes": [node] if nodes else []}
        ordered = st.ordered()
        failed = next((b for b in ordered if b.status == "failed"),
                      next((b for b in ordered if b.status != "succeeded"), None))
        if failed is None:
            return FailureDecision("halt_and_report", "no failed branch", ctx.policy)
        sibs = [view(b, True) for b in ordered if b.status == "succeeded"]
        try:
            action = _cpol.decide_on_branch_failure(_PolicyView(ctx.policy), view(failed, False), sibs)
            dec = FailureDecision(
                _ACTION_MAP[action.kind.value], action.reason, action.policy.value,
                tuple(action.compensate_nodes), tuple(action.not_reversible), tuple(action.not_compensated))
        except Exception as e:  # noqa: BLE001 - fail closed
            return FailureDecision("halt_and_report", f"policy error: {e}", "HALT_AND_REPORT")
        if self._hook is not None:                    # may only tighten
            try:
                h = self._hook(ctx, dec)
                if isinstance(h, FailureDecision):
                    if h.action == "halt_and_report":
                        dec = FailureDecision("halt_and_report", h.note or dec.note, dec.policy,
                                              (), dec.not_reversible, tuple(
                                                  list(dec.not_compensated) + list(dec.compensate_nodes)))
                    elif h.action == dec.action:
                        keep = tuple(n for n in dec.compensate_nodes if n in set(h.compensate_nodes))
                        dec = FailureDecision(dec.action, h.note or dec.note, dec.policy, keep,
                                              dec.not_reversible, dec.not_compensated)
            except Exception:  # noqa: BLE001
                pass
        return dec

    @staticmethod
    def _brief(b: BranchState) -> dict:
        return {"branch_id": b.branch_id, "node_id": b.node_id, "capability": b.capability,
                "receipt": b.receipt}

    def _apply_close(self, m: Mission, st: ForkState) -> None:
        """Idempotent mission-level effects of a closed fork (safe to repeat after a crash)."""
        rt = self.rt
        if st.closed == "satisfied":
            world = rt._world(m.id)
            published = {e.payload.get("source_ref") for e in rt.store.for_mission(m.id)
                         if e.type == "ObservationWritten"}
            for bid in st.close_payload.get("winners", []):
                b = st.branches[bid]
                ref = f"{b.node_id}"
                if b.produces and ref not in published:
                    world.observe(b.produces, b.result, source=b.operator, confidence=1.0,
                                  source_type="fork_branch", source_ref=ref)
            if m.state == MissionState.WAITING_HUMAN:
                rt._set_state(m, MissionState.RUNNING)
            return
        dec = st.close_payload.get("decision") or {}
        decision = dec.get("action", "halt_and_report")
        if decision == "continue":
            if m.state == MissionState.WAITING_HUMAN:
                rt._set_state(m, MissionState.RUNNING)
            return
        if m.state in (MissionState.FAILED, MissionState.CANCELLED, MissionState.COMPENSATION_FAILED):
            return
        plan = rt._plans[m.id]
        from . import compensation_runner as _runner  # noqa: PLC0415
        pol = _cpol.FailurePolicy(dec.get("policy") or "HALT_AND_REPORT")
        if decision == "compensate":
            self._compensate_branches(m, st, dec, pol)
        # unknown-effect branches: a failed call that actually ran on a consequential capability may have applied
        # its effect; report it, never claim it was rolled back
        unknown = [{"node_id": b.node_id, "capability": b.capability, "reversibility": "UNKNOWN_EFFECT"}
                   for b in st.ordered()
                   if b.awaiting or (b.status == "failed" and b.ran and getattr(self._caps_of(b), "side_effecting", False))]
        _runner.settle(rt, m, plan)           # compensation facts for plan nodes + every folded compensation
        out = dict(m.outcome or {})
        extra_nr = list(dec.get("not_reversible") or []) + unknown
        extra_nc = list(dec.get("not_compensated") or [])
        out["not_reversible"] = list(out.get("not_reversible") or []) + extra_nr
        out["not_compensated"] = list(out.get("not_compensated") or []) + extra_nc
        out["failure_policy"] = pol.value
        if extra_nr or extra_nc:
            out["rolled_back"] = False        # branches left as-is / irreversible / unknown effect
            # K3: the same retained-effects record as the sequential path (outcome + EffectsRetained history)
            have = {r["node_id"] for r in out.get("retained_effects") or []}
            retained = list(out.get("retained_effects") or [])
            for e in extra_nr:
                if e.get("node_id") not in have:
                    retained.append({"node_id": e.get("node_id"), "capability": e.get("capability"),
                                     "reason": ("preserved_by_declaration" if e.get("disposition") == "PRESERVE"
                                                else "unknown_effect" if e.get("reversibility") == "UNKNOWN_EFFECT"
                                                else "irreversible"), "detail": ""})
                    have.add(e.get("node_id"))
            for nid in extra_nc:
                if nid not in have:
                    b = next((x for x in st.ordered() if x.node_id == nid), None)
                    retained.append({"node_id": nid, "capability": getattr(b, "capability", ""),
                                     "reason": "left_by_policy", "detail": pol.value})
                    have.add(nid)
            out["retained_effects"] = retained
            out["partially_completed"] = True
            out["incomplete_steps"] = list(out.get("incomplete_steps") or []) + [
                {"node_id": b.node_id, "capability": b.capability, "status": "failed"}
                for b in st.ordered() if b.status == "failed"]
            _runner._record_retained(rt, m, out)
        m.outcome = out
        # NOTE: deliberately NOT rt._saga/run_saga — branches are compensated only as the policy decided above.
        rt._fail(m, plan, reason=f"fork {st.fork_id} join failed: {st.close_payload.get('reason')}")
        m.outcome = {**(m.outcome or {}), "fork": self.summary(m.id, st.fork_id), "failure_decision": decision}

    def _compensate_branches(self, m: Mission, st: ForkState, dec: dict, pol) -> None:
        """Run the DECLARED undo of the succeeded branches the policy named (reverse order), each through the
        compensation runner so every one gets its own request/authorization/execution/verification events and
        receipt. Idempotent: a branch already compensated (per the log) is skipped."""
        from . import compensation_runner as _runner  # noqa: PLC0415
        rt = self.rt
        by_node = {b.node_id: b for b in st.ordered()}
        for nid in dec.get("compensate_nodes") or []:
            b = by_node.get(nid)
            if b is None or b.status != "succeeded":
                continue
            folded = _comp.fold_compensation(rt.store.for_mission(m.id))
            if folded.get(nid, {}).get("status") in ("compensated", "executed_unverified"):
                continue
            cap = self._caps_of(b)
            undo = _comp.declared_undo(cap) if cap is not None else None
            if not undo:
                continue
            node = Node(capability=b.capability, operator=cap.operator, undo=undo, side_effecting=True,
                        idempotency_key=b.idempotency_key)
            node.id = nid
            forward_caps = {n.capability for n in rt._plans[m.id].graph.nodes} | {x.capability for x in st.ordered()}
            with _runner._lock(m.id, nid):
                _runner._compensate_node(rt, m, node, pol, forward_caps)

    def summary(self, mission_id: str, fork_id: str) -> dict:
        st = self.state(mission_id, fork_id)
        return {"fork_id": fork_id, "closed": st.closed, "join": st.join,
                "succeeded": st.ids("succeeded"), "failed": st.ids("failed"),
                "cancelled": st.ids("cancelled"), "parked": st.ids("parked"),
                "awaiting_reconciliation": [b.branch_id for b in st.ordered() if b.awaiting],
                "late_results": len(st.late_results), "generation": st.generation,
                "reasons": {b.branch_id: b.reason for b in st.ordered() if b.reason}}

    # ── subscriptions: cancellation + policy tightening (no runtime edits) ──
    def _on_event(self, ev) -> None:
        try:
            if ev.type == "MissionCancelled":
                self._cancelled.add(ev.mission_id)
                self._close_idle_forks(ev.mission_id, "mission_cancelled")
            elif ev.type == "PolicyChanged" and (ev.payload.get("changes") or {}).get("revoke"):
                self._invalidate_unstarted(ev.mission_id)
        except Exception:  # noqa: BLE001 - a subscriber must never break the log
            pass

    def _close_idle_forks(self, mission_id: str, reason: str) -> None:
        m = self.rt._missions.get(mission_id)
        with self._lock:
            evs = self.rt.store.for_mission(mission_id)
            for fid in fork_ids(evs):
                if (mission_id, fid) in self._driving:
                    continue                          # the driving loop observes the flag itself
                st = fold_fork(evs, fid)
                if st.closed or m is None:
                    continue
                for b in st.ordered():
                    if b.status not in _TERMINAL_BRANCH:
                        self._append_cancel(m, st, b, reason)
                self.rt.store.append("JoinFailed", mission_id, {
                    "fork_id": fid, "mode": st.join.get("mode"), "k": st.join.get("k"), "reason": reason,
                    "succeeded": st.ids("succeeded"), "failed": st.ids("failed"),
                    "cancelled": [b.branch_id for b in st.ordered() if b.status != "succeeded"],
                    "failure_policy": "mission_cancelled",
                    "decision": {"action": "halt_and_report", "note": "mission cancelled"}})

    def _invalidate_unstarted(self, mission_id: str) -> None:
        with self._lock:
            evs = self.rt.store.for_mission(mission_id)
            for fid in fork_ids(evs):
                st = fold_fork(evs, fid)
                if st.closed:
                    continue
                for b in st.ordered():
                    if b.approval is not None and b.status in ("pending", "retrying", "parked"):
                        self.rt.store.append("ApprovalInvalidated", mission_id, {
                            "node_id": b.node_id, "reason": "policy_tightened", "fork_id": fid})
