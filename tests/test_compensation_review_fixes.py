"""Review fixes for per-action compensation (K1-K5): every test here FAILS on the pre-fix code."""
from __future__ import annotations

import threading
import time

import pytest

from agentic_os.mission import compensation as C
from agentic_os.mission.compensation_runner import attention_items, outcome_from_log
from agentic_os.mission.fork_join import BranchSpec, ForkSpec, JoinSpec, branch_node_id
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.types import CapabilityManifest, CapabilitySpec, MissionState
from tests._fork_support import build, types
from tests.test_compensation_per_action import Fx
from tests.test_compensation_contracts import _spec, _types


# ── K1: a hand-built INSTANCE is validated like a dict ────────────────────────────────────────────────────────
def test_k1_instance_human_approval_forces_requires_approval_and_parks():
    spec = C.CompensationSpec(supported=True, capability="lock.release", verify="lock.verify",
                              preconditions="lock.safe", authorization_policy="HUMAN_APPROVAL")
    assert spec.requires_approval is False                       # as built
    assert C.coerce(spec).requires_approval is True              # normalised
    fx = Fx(lock_comp=spec)
    assert "lock.release" not in fx.calls                        # NO human, NO undo
    assert "CompensationApprovalRequired" in _types(fx.rt, fx.m)


@pytest.mark.parametrize("bad", [
    C.CompensationSpec(supported=True, capability="x", authorization_policy="EVIDENCE_BOUND", requires_approval=True),
    C.CompensationSpec(supported=True, capability="x", disposition="PRESERVE"),
    C.CompensationSpec(supported=True, capability="x", authorization_policy="TRUST_ME"),
    C.CompensationSpec(supported=False, preconditions="p"),
    C.CompensationSpec(supported=True, capability=None),
])
def test_k1_inconsistent_instances_fail_closed(bad):
    with pytest.raises(ValueError):
        C.coerce(bad)


# ── K2: EVIDENCE_BOUND can never front an approval-gated undo ──────────────────────────────────────────────────
def _k2_specs(order):
    fwd = _spec("a.do", "d", side_effecting=True, compensation={
        "supported": True, "capability": "a.undo", "authorization_policy": "EVIDENCE_BOUND"})
    undo = CapabilitySpec("a.undo", "a", side_effecting=True, approval_required=True, irreversible=True,
                          irreversible_note="n", permissions=["*"])
    return [fwd, undo] if order == "fwd_first" else [undo, fwd]


@pytest.mark.parametrize("order", ["fwd_first", "undo_first"])
def test_k2_registration_rejects_evidence_bound_over_an_approval_gated_undo(order):
    reg = CapabilityRegistry()
    with pytest.raises(ValueError, match="EVIDENCE_BOUND"):
        reg.register(CapabilityManifest("a", _k2_specs(order)))


def test_k2_cross_manifest_and_lint():
    reg = CapabilityRegistry()
    fwd, undo = _k2_specs("fwd_first")
    reg.register(CapabilityManifest("a", [fwd]))                 # target not registered yet: allowed
    with pytest.raises(ValueError, match="EVIDENCE_BOUND"):
        reg.register(CapabilityManifest("b", [undo]))            # ...but rejected when the gated target arrives
    assert any("EVIDENCE_BOUND" in p for p in C.registry_problems([fwd], {"a.undo": undo}.get))


def test_k2_runtime_never_runs_a_gated_undo_without_a_human_even_if_registered_around_the_check():
    fx = Fx(lock_comp={"authorization_policy": "HUMAN_APPROVAL"})   # the legitimate way: parks
    assert "lock.release" not in fx.calls
    from agentic_os.mission.types import CapabilitySpec as CS
    fx.rt.registry.get("lock.release").approval_required = True     # undo became gated; spec says EVIDENCE_BOUND
    fx.rt.registry.get("lock.acquire").compensation = C.CompensationSpec(
        supported=True, capability="lock.release", authorization_policy="EVIDENCE_BOUND",
        preconditions="lock.safe", verify="lock.verify")
    assert fx.rt._compensation_requires_approval("lock.acquire") is True


# ── K3: fork path honours and REPORTS PRESERVE ─────────────────────────────────────────────────────────────────
KEEP = CapabilitySpec("w.keep", "w-op", side_effecting=True, permissions=["w:write"],
                      compensation={"supported": False, "disposition": "PRESERVE", "notes": "security fix"})


def test_k3_fork_preserve_branch_is_kept_and_reported_partially_completed():
    rt, op, m = build(failure_policy="COMPENSATE_DECLARED", extra_caps=(KEEP,))
    op.fail_caps = {"w.flaky"}
    rt.forks.run_fork(m.id, ForkSpec("f1", [BranchSpec("b0", "w.keep", inputs={"tag": "b0"}),
                                            BranchSpec("b1", "w.flaky", inputs={"tag": "b1"})],
                                     JoinSpec("all"), max_concurrent=1))
    n0 = branch_node_id(m.id, "f1", "b0")
    assert not [k for k in op.calls if ":undo:" in k]             # never reversed
    o = m.outcome
    assert o["rolled_back"] is False and o["partially_completed"] is True
    assert {r["node_id"]: r["reason"] for r in o["retained_effects"]}[n0] == "preserved_by_declaration"
    assert [s["node_id"] for s in o["incomplete_steps"]]
    assert "EffectsRetained" in types(rt, m.id)


def test_k3_preserve_with_a_stray_legacy_undo_is_rejected_at_registration():
    bad = CapabilitySpec("w.bad", "w-op", side_effecting=True, undo="w.undo", permissions=["w:write"],
                         compensation={"supported": False, "disposition": "PRESERVE", "notes": "x"})
    with pytest.raises(ValueError, match="PRESERVE"):
        CapabilityRegistry().register(CapabilityManifest("w-op", [bad]))
    assert C.declared_undo(bad) is None                           # and even unregistered it is not an undo


# ── K4: a malformed remote declaration fails CLOSED (no legacy undo survives) ───────────────────────────────
def test_k4_malformed_remote_declaration_drops_the_undo():
    from agentic_os.mission.production import _spec_from_json
    spec = _spec_from_json({"name": "x.do", "operator": "x", "side_effecting": True, "undo": "x.undo",
                            "compensation": {"supported": True, "capability": "x.undo",
                                             "disposition": "PRESERVE"}})
    assert spec.undo is None and C.declared_undo(spec) is None
    assert C.reversibility_of(spec) is C.Reversibility.IRREVERSIBLE
    ok = _spec_from_json({"name": "y.do", "operator": "y", "side_effecting": True, "undo": "y.undo"})
    assert ok.undo == "y.undo"                                    # a legacy declaration without `compensation` is unchanged


# ── K5: truncated log, cancel, hung precondition ───────────────────────────────────────────────────────────────
def test_k5_eligible_but_never_compensated_node_is_not_rolled_back_on_replay():
    fx = Fx(report_ok=False, lock_comp={})
    rt, mid = fx.rt, fx.m.id
    # truncate the log: drop every Compensation*/NodeCompensated record (the saga "never ran")
    kept = [e for e in rt.store.for_mission(mid) if not (e.type.startswith("Compensation") or e.type in
                                                         ("NodeCompensated", "EffectsRetained"))]
    from agentic_os.mission.store import EventStore
    st2 = EventStore()
    for e in kept:
        st2.append(e.type, mid, e.payload)
    from agentic_os.mission.runtime import MissionRuntime
    rt2 = MissionRuntime(rt.registry, rt.executor, store=st2, planner=rt.planner)
    rt2.rehydrate(mid, policy_refs=["*"])
    out = outcome_from_log(rt2, mid)
    assert out["rolled_back"] is False
    assert any(r["reason"] == "not_yet_compensated" for r in out["retained_effects"])


def test_k5_cancelling_a_mission_that_holds_an_effect_records_it():
    from tests.test_compensation_per_action import OUTCOMES  # noqa: F401
    fx = Fx(holds=True)
    # take a second mission to the point where the lock is held, then cancel it
    from agentic_os.mission.types import MissionState as MS
    m2 = fx.rt.create_mission("again", policy_refs=["*"])
    fx.rt.run(m2.id)
    nodes_done = [n for n in fx.rt._plans[m2.id].graph.nodes
                  if fx.rt.repo.node_status(m2.id).get(n.id).value == "done" and n.side_effecting]
    assert nodes_done                                               # lock acquired, parked on the next step
    fx.rt.cancel_mission(m2.id, reason="operator cancelled")
    assert "EffectsRetained" in _types(fx.rt, m2)
    assert {r["capability"] for r in m2.outcome["retained_effects"]} >= {"lock.acquire"}


def test_k5_hung_precondition_times_out_and_fails_closed(monkeypatch):
    monkeypatch.setenv(C.ENV_PRECONDITION_TIMEOUT, "0.2")
    gate = threading.Event()

    def hang(i):
        gate.wait(5)
        return {"holds": True}

    t0 = time.time()
    fx = Fx(safe=hang)
    gate.set()
    assert time.time() - t0 < 3
    assert "lock.release" not in fx.calls
    assert "timed out" in next(iter(fx.m.outcome["compensation_skipped"].values()))
