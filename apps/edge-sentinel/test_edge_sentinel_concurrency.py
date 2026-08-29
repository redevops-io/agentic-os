"""Safe-concurrency guard for the edge-sentinel operator (parallel-execution migration, step 4)."""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("edge-sentinel.operator").build_edge_sentinel_operator()
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


def test_block_and_unblock_same_ip_serialize():
    n, reason = _reason(_node("a", "sentinel.block_ip", ip="1.2.3.4"),
                        _node("b", "sentinel.unblock_ip", ip="1.2.3.4"))
    assert n == 1 and "sentinel:ip:1.2.3.4" in reason


def test_actions_on_different_ips_parallelize():
    assert _released(_node("a", "sentinel.block_ip", ip="1.2.3.4"),
                     _node("b", "sentinel.block_ip", ip="5.6.7.8")) == {"a", "b"}


def test_triage_is_read_only():
    assert _SPECS["sentinel.triage"].concurrency_mode == "read_only"
