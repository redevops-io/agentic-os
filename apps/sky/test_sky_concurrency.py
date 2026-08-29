"""Safe-concurrency guard for the sky operator (parallel-execution migration, step 4)."""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("sky.operator").build_sky_operator()
_SPECS = {c.name: c for c in _op.manifest.capabilities}
_S, _W = TopoScheduler(), SchedulePolicy(max_concurrency=8)


def _node(nid, cap, **inp):
    s = _SPECS[cap]
    n = Node(capability=s.name, operator=s.operator, side_effecting=s.side_effecting,
             concurrency_mode=s.concurrency_mode, concurrency_key=s.concurrency_key,
             resource_keys=list(s.resource_keys), max_parallelism=s.max_parallelism, inputs=dict(inp))
    n.id = nid
    return n


def _released(*ns):
    return {n.id for n in _S.ready(ExecutionGraph(nodes=list(ns)), set(), set(), _W)}


def _reason(*ns):
    g = ExecutionGraph(nodes=list(ns))
    ser = [r for r in _S.explain(g, set(), set(), _W) if r["decision"] == "serialized"]
    return len(_S.ready(g, set(), set(), _W)), (ser[0]["reason"] if ser else "")


def test_launches_to_different_clusters_parallelize():
    assert _released(_node("a", "sky.launch", cluster="c1"), _node("b", "sky.launch", cluster="c2")) == {"a", "b"}


def test_launch_and_down_on_the_same_cluster_serialize():
    n, reason = _reason(_node("a", "sky.launch", cluster="c1"), _node("b", "sky.down", cluster="c1"))
    assert n == 1 and "sky:cluster:c1" in reason


def test_read_only_caps_never_block():
    ro = ["sky.optimize", "sky.status", "sky.check"]
    assert len(_released(*[_node(f"n{i}", c) for i, c in enumerate(ro)])) == 3
