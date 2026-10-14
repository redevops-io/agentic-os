"""Fork/join hardening: single driver (no duplicate dispatch), budget cannot be bypassed, produces is scoped,
spec validation, slot release, retry safety, restart resume, nits."""
from __future__ import annotations

import math
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from agentic_os.mission.fork_join import (
    BranchSpec, ForkError, ForkJoinController, ForkLimits, ForkSpec, JoinSpec, branch_node_id,
    is_branch_node_id)
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import CapabilitySpec, MissionState, NodeCost
from tests._fork_support import GRANTS, CountingOperator, build, make_rt, spec, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class NonIdempotentOperator:
    """Counts REAL effects outside the log and does NOT dedupe on the idempotency key (worst case)."""

    def __init__(self, delay=0.05):
        self.delay, self.lock, self.effects, self.keys = delay, threading.Lock(), [], []

    def invoke(self, operator, capability, inputs, key, *, secrets=None):
        if not capability.startswith("w."):
            return {"ok": True}
        time.sleep(self.delay)
        with self.lock:
            self.effects.append(inputs.get("tag"))
            self.keys.append(key)
        return {"ok": True, "tag": inputs.get("tag")}


def _starts(rt, mid):
    return [(e.payload["branch_id"], e.payload["attempt"]) for e in rt.store.for_mission(mid)
            if e.type == "BranchStarted"]


# ───────────── 1. single driver ─────────────
def test_two_threads_on_one_fork_dispatch_each_branch_exactly_once():
    op = NonIdempotentOperator()
    rt, _, m = build(op=op)
    s = spec(n=3)
    errs = []

    def go():
        try:
            rt.forks.run_fork(m.id, s)
        except BaseException as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert not errs, errs
    assert sorted(op.effects) == ["b0", "b1", "b2"] and len(_starts(rt, m.id)) == 3


def test_two_controller_instances_on_one_log_dispatch_exactly_once():
    store = EventStore()
    op = NonIdempotentOperator()
    rt1, _, m = build(store=store, op=op)
    rt2 = make_rt(store, op)
    rt2._missions, rt2._plans = rt1._missions, rt1._plans
    s = spec(n=3)
    errs = []

    def go(rt):
        try:
            rt.forks.run_fork(m.id, s)
        except BaseException as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=go, args=(r,)) for r in (rt1, rt2)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert not errs, errs
    assert sorted(op.effects) == ["b0", "b1", "b2"] and len(_starts(rt1, m.id)) == 3


_PROC = textwrap.dedent("""
    import sys, time, os
    from tests._fork_support import make_rt, spec, GRANTS
    from agentic_os.mission.store import EventStore
    from agentic_os.mission.fork_join import ForkError
    path, mid, role, effects = sys.argv[1:5]
    class Op:
        def invoke(self, operator, capability, inputs, key, *, secrets=None):
            if not capability.startswith("w."):
                return {"ok": True}
            time.sleep(1.5)
            fd = os.open(effects, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            os.write(fd, (inputs["tag"] + "\\n").encode()); os.close(fd)
            return {"ok": True}
    rt = make_rt(EventStore(path), Op())
    rt.rehydrate(mid, policy_refs=GRANTS)
    if role == "B":                       # wait until A has claimed the fork
        for _ in range(400):
            if "ForkDriverClaimed" in open(path).read():
                break
            time.sleep(0.05)
    try:
        r = rt.forks.run_fork(mid, spec(n=3))
        print("DONE", r.status)
    except ForkError as e:
        print("REFUSED", str(e)[:60])
""")


def test_second_process_is_refused_and_each_branch_dispatches_once(tmp_path):
    path, effects = str(tmp_path / "log.jsonl"), str(tmp_path / "effects.txt")
    rt, _, m = build(store=EventStore(path))
    env = {**os.environ, "PYTHONPATH": ROOT}
    run = lambda role: subprocess.Popen([sys.executable, "-c", _PROC, path, m.id, role, effects],  # noqa: E731
                                        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    a = run("A")
    time.sleep(0.2)
    b = run("B")
    ao, ae = a.communicate(timeout=60)
    bo, be = b.communicate(timeout=60)
    assert "DONE satisfied" in ao, (ao, ae[-500:])
    assert "REFUSED" in bo, (bo, be[-500:])
    assert sorted(open(effects).read().split()) == ["b0", "b1", "b2"]
    fresh = EventStore(path)
    assert len([e for e in fresh.for_mission(m.id) if e.type == "BranchStarted"]) == 3


def test_claim_generation_increases_and_restart_orphans_are_redispatched_only_by_newer_generation():
    from agentic_os.mission.fork_join import fold_fork
    rt, op, m = build()
    s = spec(n=2, max_concurrent=1)
    rt.forks.run_fork(m.id, s)
    claims = [e.payload for e in rt.store.for_mission(m.id) if e.type == "ForkDriverClaimed"]
    assert [c["generation"] for c in claims][:1] == [1]
    st = fold_fork(rt.store.for_mission(m.id), "f1")
    assert st.generation == 1 and all(b.generation == 1 for b in st.ordered())


# ───────────── 2. budget cannot be bypassed ─────────────
def test_branch_spec_has_no_cost_override():
    with pytest.raises(TypeError):
        BranchSpec("a", "w.write", cost=NodeCost(usd=0))


def test_capability_cost_is_always_what_is_reserved():
    rt, op, m = build(fork_budget=0.05)
    res = rt.forks.run_fork(m.id, spec(n=1))
    assert res.state.branches["b0"].reason == "budget_exceeded" and op.calls == {}


def test_eleven_branches_cannot_exceed_a_tiny_budget():
    rt, op, m = build(fork_budget=0.25, concurrency=4)
    brs = [BranchSpec(f"b{i}", "w.write", inputs={"tag": f"b{i}"}) for i in range(11)]
    rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec("quorum", 1, cancel_losers=False)))
    assert len(op.effects) <= 2


@pytest.mark.parametrize("usd", [float("nan"), float("inf"), -1000.0, -0.01])
def test_invalid_capability_cost_is_rejected(usd):
    bad = CapabilitySpec("w.bad", "w-op", permissions=["w:write"], cost=NodeCost(usd=usd))
    rt, op, m = build(extra_caps=(bad,))
    with pytest.raises(ForkError, match="invalid cost"):
        rt.forks.run_fork(m.id, ForkSpec("f1", [BranchSpec("a", "w.bad")], JoinSpec()))
    assert op.calls == {} and "ForkRejected" in types(rt, m.id)


def test_nan_mission_budget_refuses_branches():
    rt, op, m = build()
    m.budget.usd = float("nan")
    res = rt.forks.run_fork(m.id, spec(n=1))
    assert res.state.branches["b0"].reason == "budget_exceeded" and op.calls == {}


# ───────────── 3. produces ─────────────
def test_produces_must_be_provided_by_the_capability():
    rt, op, m = build()
    for key in ("consent_filed", "subscription", "w_out2"):
        with pytest.raises(ForkError, match="not provided"):
            rt.forks.run_fork(m.id, ForkSpec("f1", [BranchSpec("a", "w.write", produces=key)], JoinSpec()))
    assert op.calls == {}


def test_published_observation_is_provenanced_to_the_branch():
    rt, op, m = build()
    rt.forks.run_fork(m.id, ForkSpec("f1", [BranchSpec("a", "w.write", inputs={"tag": "a"}, produces="w_out")], JoinSpec()))
    obs = [e.payload for e in rt.store.for_mission(m.id) if e.type == "ObservationWritten"]
    assert obs[0]["key"] == "w_out" and obs[0]["source_ref"] == branch_node_id(m.id, "f1", "a")
    assert obs[0]["source_type"] == "fork_branch"


# ───────────── 5/6. validation ─────────────
@pytest.mark.parametrize("mk", [
    lambda: spec(timeout_s=float("nan")), lambda: spec(timeout_s="abc"), lambda: spec(timeout_s=True),
    lambda: spec(timeout_s=float("inf")), lambda: spec(timeout_s=0), lambda: spec(timeout_s=-1),
    lambda: spec(mode="quorum", k=True), lambda: spec(mode="quorum", k=1.0), lambda: spec(mode="any", k=2),
    lambda: ForkSpec("f\n", [BranchSpec("a", "w.write")], JoinSpec()),
    lambda: ForkSpec("f", [BranchSpec("a\n", "w.write")], JoinSpec()),
    lambda: ForkSpec(None, [BranchSpec("a", "w.write")], JoinSpec()),
    lambda: ForkSpec("f", [BranchSpec("a", "w.write", inputs="x")], JoinSpec()),
    lambda: ForkSpec("f", [BranchSpec("a", "w.write", max_attempts=True)], JoinSpec()),
    lambda: ForkSpec("f", ["notabranch"], JoinSpec()),
    lambda: spec(max_concurrent=0),
])
def test_every_invalid_spec_is_rejected_with_a_logged_forkrejected(mk):
    rt, op, m = build()
    with pytest.raises(ForkError):
        rt.forks.run_fork(m.id, mk())
    assert op.calls == {} and "ForkRejected" in types(rt, m.id) and "ForkOpened" not in types(rt, m.id)


def test_nan_timeout_cannot_mean_no_timeout():
    rt, op, m = build()
    gate = threading.Event()
    op.gates["b0"] = gate
    with pytest.raises(ForkError):
        rt.forks.run_fork(m.id, spec(n=1, timeout_s=float("nan")))
    gate.set()
    assert op.calls == {}


def test_repeated_identical_rejection_does_not_grow_the_log():
    rt, op, m = build()
    for _ in range(6):
        with pytest.raises(ForkError):
            rt.forks.run_fork(m.id, spec(n=17))
    assert types(rt, m.id).count("ForkRejected") == 1


def test_newline_ids_and_none_node_ids():
    rt, op, m = build()
    assert not is_branch_node_id(None) and rt.forks.owns(m.id, None) is False
    assert not is_branch_node_id("m:fork:f:b") is False


# ───────────── 7. slot release ─────────────
@pytest.mark.parametrize("where", ["inputs", "branch_started", "submit"])
def test_slot_released_when_anything_between_acquire_and_submit_raises(where, monkeypatch):
    rt, op, m = build(limits=ForkLimits())
    c = rt.forks
    if where == "inputs":
        monkeypatch.setattr(c, "_inputs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    elif where == "submit":
        monkeypatch.setattr(c, "_submit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    else:
        orig = rt.store.append

        def bad(type, mid, payload):
            if type == "BranchStarted":
                raise RuntimeError("boom")
            return orig(type, mid, payload)
        monkeypatch.setattr(rt.store, "append", bad)
    with pytest.raises(RuntimeError, match="boom"):
        c.run_fork(m.id, spec(n=3))
    assert all(v == 0 for v in c._tenant_slots.values()), c._tenant_slots


# ───────────── 9. restart + register_spec ─────────────
def test_post_restart_approval_resumes_only_with_a_matching_registered_spec(tmp_path):
    path = str(tmp_path / "log.jsonl")
    rt, op, m = build(store=EventStore(path))
    s = ForkSpec("f1", [BranchSpec("g", "w.gated", inputs={"tag": "g"})], JoinSpec())
    assert rt.forks.run_fork(m.id, s).status == "parked"
    rt2 = make_rt(EventStore(path), op)
    rt2.rehydrate(m.id, policy_refs=GRANTS)
    node = branch_node_id(m.id, "f1", "g")
    with pytest.raises(ForkError, match="spec_drift"):
        rt2.forks.register_spec(m.id, ForkSpec("f1", [BranchSpec("g", "w.gated", inputs={"tag": "CHANGED"})], JoinSpec()))
    with pytest.raises(ForkError, match="spec_drift"):
        rt2.forks.run_fork(m.id, ForkSpec("f1", [BranchSpec("g", "w.gated", inputs={"tag": "CHANGED"})], JoinSpec()))
    rt2.forks.register_spec(m.id, s)
    rt2.approve(m.id, node, "approve")
    assert rt2.forks.state(m.id, "f1").closed == "satisfied" and len(op.effects) == 1


def test_branch_decisions_other_than_approve_reject_are_refused():
    rt, op, m = build()
    s = ForkSpec("f1", [BranchSpec("g", "w.gated", inputs={"tag": "g"})], JoinSpec())
    rt.forks.run_fork(m.id, s)
    with pytest.raises(ValueError):
        rt.approve(m.id, branch_node_id(m.id, "f1", "g"), "accept")
    assert rt.forks.state(m.id, "f1").ids("parked") == ["g"]


def test_rt_run_does_not_drive_forks():
    rt, op, m = build()
    rt.forks._open(m, spec(n=2))
    rt.run(m.id)
    assert op.calls == {} and "BranchStarted" not in types(rt, m.id)    # plan nodes ran; the fork did not


# ───────────── 10. nits ─────────────
def test_from_env_survives_unicode_digits(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_FORK_MAX_CONCURRENT", "²")
    monkeypatch.setenv("AGENTIC_OS_FORK_MAX_BRANCHES", "abc")
    assert ForkLimits.from_env().max_concurrent_branches == 4 and ForkLimits.from_env().max_branches == 16
