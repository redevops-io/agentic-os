"""Mission fork/join — parallel branches *within* an existing Mission (docs/MISSION_FORK_JOIN.md).

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
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import concurrency as conc
from .approval import is_approval_key, strip_approval_keys
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
    max_attempts: int = 1
    cost: NodeCost | None = None       # overrides the capability's declared cost for budget reservation


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
    failure_policy: str | None = None
    cancel_siblings_on_branch_cancel: bool = False

    def digest(self) -> str:
        return _sha({
            "fork_id": self.fork_id,
            "branches": [{"id": b.branch_id, "cap": b.capability, "requires": sorted(b.requires),
                          "produces": b.produces, "inputs_sha256": _sha(b.inputs),
                          "max_attempts": b.max_attempts,
                          "cost": (vars(b.cost) if b.cost is not None else None)} for b in self.branches],
            "join": vars(self.join), "max_concurrent": self.max_concurrent,
            "failure_policy": self.failure_policy,
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
            raw = os.environ.get(env, "").strip()
            if raw.isdigit() and int(raw) > 0:
                setattr(lim, key, int(raw))
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
    action: str = "halt_and_report"    # halt_and_report | compensate | continue
    note: str = ""


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


class _HaltAndReport:
    name = "halt_and_report"

    def __call__(self, ctx: FailureContext) -> FailureDecision:
        return FailureDecision("halt_and_report", "no auto-compensation; mission reports branch outcomes")


HALT_AND_REPORT = _HaltAndReport()


# ───────────────────────────── fold ─────────────────────────────
@dataclass
class BranchState:
    branch_id: str
    node_id: str = ""
    capability: str = ""
    operator: str = ""
    produces: str | None = None
    max_attempts: int = 1
    status: str = "pending"            # pending|running|parked|retrying|succeeded|failed|cancelled
    attempt: int = 0
    idempotency_key: str = ""
    reason: str = ""
    ran: bool = False
    result: Any = None
    receipt: dict | None = None
    approval: str | None = None        # capability an unrevoked human approval is bound to
    parked: bool = False
    reserved: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"branch_id": self.branch_id, "node_id": self.node_id, "capability": self.capability,
                "status": self.status, "attempt": self.attempt, "idempotency_key": self.idempotency_key,
                "reason": self.reason, "ran": self.ran, "result": self.result, "receipt": self.receipt,
                "approval": self.approval, "parked": self.parked}


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
        if not st.opened:
            continue
        if t in ("NodeParked", "NodeResumed", "NodeSkipped", "ApprovalGranted", "ApprovalInvalidated",
                 "ApprovalRejected"):
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
    status: str                     # satisfied | failed | parked
    state: ForkState

    @property
    def results(self) -> dict:
        return {b.branch_id: b.result for b in self.state.ordered() if b.status == "succeeded"}


class SimulatedCrash(BaseException):
    """Raised by test harnesses to model a process crash; never swallowed by the controller."""


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
        self._policies: dict[str, Callable] = {"halt_and_report": HALT_AND_REPORT}
        self._default_policy = "halt_and_report"
        self._cancelled: set = set()
        self._tenants: dict[str, str] = {}
        runtime.store.subscribe(self._on_event)

    # ── public hooks ──
    def on_branch_failure(self, policy: Callable, name: str | None = None) -> None:
        """Plug a failure policy (e.g. the compensation agent's). ``policy(FailureContext) -> FailureDecision``.
        Registered under ``name`` (default ``policy.name``) and made the default for forks that do not name
        one. Without a call the default is HALT_AND_REPORT (no auto-compensation)."""
        nm = name or getattr(policy, "name", None) or "custom"
        self._policies[nm] = policy
        self._default_policy = nm

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
        closes or parks on a human approval. Idempotent: a closed fork returns its stored result."""
        rt = self.rt
        m = rt._missions[mission_id]
        done = self.state(mission_id, spec.fork_id)
        if done.closed:                      # already decided: only re-apply the (idempotent) mission effects
            if done.spec_digest != spec.digest():
                raise ForkError(f"fork {spec.fork_id} spec differs from the recorded spec_digest")
            self._apply_close(m, done)
            return ForkResult(done.closed, done)
        if m.state in (MissionState.CANCELLED, MissionState.FAILED, MissionState.SUCCEEDED):
            raise ForkError(f"mission {mission_id} is {m.state.value}; cannot run a fork")
        self._specs[(mission_id, spec.fork_id)] = spec
        nodes = self._open(m, spec)
        return self._drive(m, spec, nodes)

    resume_fork = run_fork

    def _tenant(self, m: Mission) -> str:
        t = getattr(m, "tenant", "") or self._tenants.get(m.id)
        if t is None:      # a rehydrated Mission does not carry the tenant; MissionCreated does
            t = self._tenants[m.id] = str(self.rt.repo.created(m.id).get("tenant") or "")
        return t

    def _validate(self, m: Mission, spec: ForkSpec, lim: dict) -> None:
        if not _ID_RE.match(spec.fork_id or ""):
            raise ForkError("invalid fork_id")
        ids = [b.branch_id for b in spec.branches]
        if not ids:
            raise ForkError("fork has no branches")
        if any(not _ID_RE.match(i or "") for i in ids) or len(set(ids)) != len(ids):
            raise ForkError("branch ids must be unique and match [A-Za-z0-9_.-]{1,64}")
        if len(ids) > lim["max_branches"]:
            raise ForkError(f"{len(ids)} branches exceeds max_branches={lim['max_branches']}")
        j = spec.join
        if j.mode not in MODES:
            raise ForkError(f"unknown join mode {j.mode!r}")
        if j.mode == "quorum" and not (isinstance(j.k, int) and 1 <= j.k <= len(ids)):
            raise ForkError("quorum requires 1 <= k <= number of branches")
        if j.on_timeout not in ("fail", "partial"):
            raise ForkError("on_timeout must be 'fail' or 'partial'")
        if any(b.max_attempts < 1 for b in spec.branches):
            raise ForkError("max_attempts must be >= 1")
        scoped = self.rt._scoped(m)
        for b in spec.branches:
            for cap in (b.capability, *b.requires):
                if scoped.get(cap) is None:
                    raise ForkError(f"capability {cap!r} is not granted to mission {m.id}")
            if not set(b.requires) <= {b.capability} | set(b.requires):  # pragma: no cover - structural
                raise ForkError("invalid requires")

    def _open(self, m: Mission, spec: ForkSpec) -> dict:
        rt = self.rt
        with self._lock:
            evs = rt.store.for_mission(m.id)
            existing = fold_fork(evs, spec.fork_id)
            lim = self.limits.for_tenant(self._tenant(m))
            if existing.opened:
                if existing.spec_digest != spec.digest():
                    raise ForkError(f"fork {spec.fork_id} spec differs from the recorded spec_digest")
            else:
                try:
                    self._validate(m, spec, lim)
                    open_forks = [f for f in fork_ids(evs) if fold_fork(evs, f).closed is None]
                    if len(open_forks) >= lim["max_open_forks"]:
                        raise ForkError(f"max_open_forks={lim['max_open_forks']} reached")
                except ForkError as e:
                    rt.store.append("ForkRejected", m.id, {"fork_id": spec.fork_id, "reason": str(e)})
                    raise
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
                                "max_attempts": b.max_attempts,
                                "cost": vars(b.cost) if b.cost is not None else None})
                rt.store.append("ForkOpened", m.id, {
                    "fork_id": spec.fork_id, "spec_digest": spec.digest(), "branches": brs,
                    "join": vars(spec.join), "failure_policy": spec.failure_policy,
                    "cancel_siblings_on_branch_cancel": spec.cancel_siblings_on_branch_cancel,
                    "limits": {**lim, "effective_concurrency": eff, "tenant": self._tenant(m),
                               "bound_by": ("tenant_limit" if lim["max_concurrent_branches"] <= conc_req
                                            else "fork_request")},
                    "opened_at": opened, "deadline": dl})
        # branch Node objects (runtime-only; rebuilt identically on every resume)
        nodes: dict[str, Node] = {}
        for b in spec.branches:
            cap = rt.registry.get(b.capability)
            node = Node(capability=b.capability, operator=cap.operator, produces=b.produces,
                        approval_required=cap.approval_required, side_effecting=cap.side_effecting,
                        undo=cap.undo, inputs={}, cost=b.cost or NodeCost(
                            usd=cap.cost.usd, latency_ms=cap.cost.latency_ms, tokens=cap.cost.tokens,
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
    def _drive(self, m: Mission, spec: ForkSpec, nodes: dict) -> ForkResult:
        rt = self.rt
        fid = spec.fork_id
        key = (m.id, fid)
        by_id = {b.branch_id: b for b in spec.branches}
        with self._lock:
            self._driving.add(key)
        futs: dict[str, _cf.Future] = {}
        try:
            if m.state != MissionState.RUNNING:
                rt._set_state(m, MissionState.RUNNING)
            while True:
                st = self.state(m.id, fid)
                if st.closed:
                    self._apply_close(m, st)
                    return ForkResult(st.closed, st)
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
            if bs.status not in ("pending", "retrying") and not (
                    bs.status == "running" and bs.branch_id not in futs):
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
                if decision.required:
                    self._park(m, node, decision, st.fork_id, bs.branch_id)
                    started = True
                    continue
            if not rt._pre_exec_ok(m, plan, node):
                self._append_failed(m, st, bs, attempt, "admission_refused", ran=False)
                started = True
                continue
            if not self._acquire_slot(m, lim):
                break
            b_spec = by_id[bs.branch_id]
            idem = branch_idempotency_key(m.id, st.fork_id, bs.branch_id, attempt)
            with self._lock:                          # atomic: budget check + reservation + durable start
                reserved = {} if resumed else {"usd": node.cost.usd, "tokens": node.cost.tokens}
                if not resumed:
                    usd, tok = self._spent(m)
                    over = (usd + node.cost.usd > m.budget.usd + 1e-9
                            or (node.cost.tokens and tok + node.cost.tokens > m.budget.tokens))
                    if over:
                        self._release_slot(m)
                        self._append_failed(m, st, bs, attempt, "budget_exceeded", ran=False)
                        started = True
                        continue
                rt.store.append("BranchStarted", m.id, {
                    "fork_id": st.fork_id, "branch_id": bs.branch_id, "node_id": node.id,
                    "attempt": attempt, "idempotency_key": idem, "reserved": reserved,
                    "resumed": resumed})
            node.idempotency_key = idem
            inputs = self._inputs(m, b_spec, node, bs)
            fut = self._submit(m, node, inputs)
            fut.add_done_callback(lambda _f, mm=m: self._release_slot(mm))
            futs[bs.branch_id] = fut
            conc.acquire(node, inflight)
            ops[node.operator] = ops.get(node.operator, 0) + 1
            started = True
        return started

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
        m = rt._missions[mission_id]
        evs = rt.store.for_mission(mission_id)
        target = None
        for fid in fork_ids(evs):
            st = fold_fork(evs, fid)
            for b in st.ordered():
                if b.node_id == node_id and st.closed is None:
                    target = (st, b)
        if target is None or not target[1].parked:
            return m                                 # not a parked branch of an open fork: no-op, fail closed
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
            self._append_failed(m, st, bs, attempt, "operator_error", ran=True, error=str(exc),
                                will_retry=attempt < b_spec.max_attempts)
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
            rt.store.append("BranchSucceeded", m.id, {
                "fork_id": st.fork_id, "branch_id": bs.branch_id, "node_id": node.id, "attempt": attempt,
                "result": result, "receipt": receipt})
        rt._meter(m, node, "ok")
        rt.learning.record_capability(node.capability, True)

    def _append_failed(self, m, st, bs, attempt, reason, *, ran, error="", will_retry=False,
                       receipt=None, result=None) -> None:
        payload = {"fork_id": st.fork_id, "branch_id": bs.branch_id, "node_id": bs.node_id,
                   "attempt": attempt, "reason": reason, "error": error, "ran": ran,
                   "will_retry": bool(will_retry)}
        if receipt is not None:
            payload["receipt"] = receipt
        if result is not None:
            payload["result"] = result
        with self._lock:
            self.rt.store.append("BranchFailed", m.id, payload)

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
        self.rt.store.append("BranchCancelled", m.id, {
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
        except BaseException:  # noqa: BLE001 - a late callback must never raise (nor kill the worker)
            pass

    # ── close ──
    def _finalize(self, m: Mission, st: ForkState, kind: str, *, reason: str, partial: bool = False) -> None:
        rt = self.rt
        c = st.counts()
        brief = lambda ids: [self._brief(st.branches[i]) for i in ids]  # noqa: E731
        with self._lock:
            if self.state(m.id, st.fork_id).closed:
                return
            if kind == "satisfied":
                rt.store.append("JoinSatisfied", m.id, {
                    "fork_id": st.fork_id, "mode": st.join.get("mode"), "k": st.join.get("k"),
                    "winners": _winners(st), "failed": st.ids("failed"), "cancelled": st.ids("cancelled"),
                    "abandoned": [], "partial": partial, "reason": reason})
            else:
                ctx = FailureContext(
                    mission_id=m.id, fork_id=st.fork_id, join_mode=st.join.get("mode", "all"), reason=reason,
                    succeeded=brief(st.ids("succeeded")), failed=brief(st.ids("failed")),
                    cancelled=brief(st.ids("cancelled")), tenant=self._tenant(m))
                pname = st.failure_policy or self._default_policy
                policy = self._policies.get(pname, HALT_AND_REPORT)
                try:
                    decision = policy(ctx)
                    if not isinstance(decision, FailureDecision) or decision.action not in (
                            "halt_and_report", "compensate", "continue"):
                        raise ValueError("invalid decision")
                except Exception as e:  # noqa: BLE001 - fail closed to the safe default
                    decision = FailureDecision("halt_and_report", f"policy error: {e}")
                rt.store.append("JoinFailed", m.id, {
                    "fork_id": st.fork_id, "mode": st.join.get("mode"), "k": st.join.get("k"),
                    "reason": reason, "succeeded": st.ids("succeeded"), "failed": st.ids("failed"),
                    "cancelled": st.ids("cancelled"), "failure_policy": pname,
                    "decision": {"action": decision.action, "note": decision.note}})

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
                                  source_ref=ref)
            if m.state == MissionState.WAITING_HUMAN:
                rt._set_state(m, MissionState.RUNNING)
            return
        decision = (st.close_payload.get("decision") or {}).get("action", "halt_and_report")
        if decision == "continue":
            if m.state == MissionState.WAITING_HUMAN:
                rt._set_state(m, MissionState.RUNNING)
            return
        if m.state in (MissionState.FAILED, MissionState.CANCELLED):
            return
        plan = rt._plans[m.id]
        # NOTE: deliberately NOT rt._saga — successful branches are never auto-compensated.
        rt._fail(m, plan, reason=f"fork {st.fork_id} join failed: {st.close_payload.get('reason')}")
        m.outcome = {**(m.outcome or {}), "fork": self.summary(m.id, st.fork_id),
                     "failure_decision": decision}

    def summary(self, mission_id: str, fork_id: str) -> dict:
        st = self.state(mission_id, fork_id)
        return {"fork_id": fork_id, "closed": st.closed, "join": st.join,
                "succeeded": st.ids("succeeded"), "failed": st.ids("failed"),
                "cancelled": st.ids("cancelled"), "parked": st.ids("parked"),
                "late_results": len(st.late_results),
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
