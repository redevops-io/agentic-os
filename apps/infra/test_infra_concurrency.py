"""Safe-concurrency guard for the infra deployment operator (parallel-execution migration, step 4).

Proves — through the real TopoScheduler, from the operator's DECLARED concurrency metadata — that:
  * deployments to INDEPENDENT targets (different cloud / inventory) run concurrently;
  * deployments to the SAME target serialize, with an auditable resource-key reason;
  * read-only plan/verify/drift never block each other.

A paired overlap/conflict assertion, not a timing threshold.
"""
from __future__ import annotations

from infra.operator import build_infra_operator

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_SPECS = {c.name: c for c in build_infra_operator().manifest.capabilities}
_SCHED = TopoScheduler()
_WIDE = SchedulePolicy(max_concurrency=8)   # cap wide enough that ONLY resource conflicts bind


def _node(nid: str, cap_name: str, **inputs) -> Node:
    """Build the physical node the compiler would emit for this capability — carrying its declared
    concurrency semantics — with concrete step inputs so `concurrency_key` resolves."""
    spec = _SPECS[cap_name]
    n = Node(capability=spec.name, operator=spec.operator, side_effecting=spec.side_effecting,
             concurrency_mode=spec.concurrency_mode, concurrency_key=spec.concurrency_key,
             resource_keys=list(spec.resource_keys), max_parallelism=spec.max_parallelism,
             inputs=dict(inputs))
    n.id = nid
    return n


def _released(*nodes):
    return {n.id for n in _SCHED.ready(ExecutionGraph(nodes=list(nodes)), done=set(), running=set(), policy=_WIDE)}


# ── the manifest declares the right conflict semantics ──────────────────────────────────────────────────

def test_capabilities_declare_conflict_semantics():
    assert _SPECS["infra.provision"].concurrency_key == "tf:state:{cloud}"
    assert _SPECS["infra.destroy_delta"].concurrency_key == "tf:state:{cloud}"   # same lock as apply
    assert _SPECS["infra.configure"].concurrency_key == "ansible:inventory:{inventory}"
    assert _SPECS["infra.rollback_release"].concurrency_key == "ansible:inventory:{inventory}"
    for ro in ("infra.plan", "infra.verify", "infra.drift"):
        assert _SPECS[ro].concurrency_mode == "read_only", ro


# ── overlap: independent targets run concurrently ───────────────────────────────────────────────────────

def test_provisions_to_independent_clouds_parallelize():
    a = _node("aws", "infra.provision", cloud="aws")
    g = _node("gcp", "infra.provision", cloud="gcp")
    assert _released(a, g) == {"aws", "gcp"}, "independent-cloud provisions must run concurrently"


def test_configures_to_independent_inventories_parallelize():
    a = _node("h1", "infra.configure", inventory="prod-us")
    b = _node("h2", "infra.configure", inventory="prod-eu")
    assert _released(a, b) == {"h1", "h2"}


# ── conflict: the same target serializes, with a reason ─────────────────────────────────────────────────

def test_provisions_to_the_same_cloud_serialize_with_a_reason():
    a = _node("aws1", "infra.provision", cloud="aws")
    b = _node("aws2", "infra.provision", cloud="aws")
    g = ExecutionGraph(nodes=[a, b])
    released = _SCHED.ready(g, done=set(), running=set(), policy=_WIDE)
    assert len(released) == 1, "two applies to one terraform state must serialize"
    rows = _SCHED.explain(g, done=set(), running=set(), policy=_WIDE)
    serialized = next(r for r in rows if r["decision"] == "serialized")
    assert "tf:state:aws" in serialized["reason"]


def test_apply_and_destroy_on_the_same_state_serialize():
    """destroy_delta shares the tf:state lock with provision → an apply and a compensating destroy on the
    same cloud never run at once."""
    a = _node("apply", "infra.provision", cloud="aws")
    d = _node("destroy", "infra.destroy_delta", cloud="aws")
    assert len(_released(a, d)) == 1


# ── read-only work never blocks ─────────────────────────────────────────────────────────────────────────

def test_read_only_plans_on_the_same_cloud_all_run():
    plans = [_node(f"p{i}", "infra.plan", cloud="aws") for i in range(3)]
    assert _released(*plans) == {"p0", "p1", "p2"}, "read-only plans must not block each other"
