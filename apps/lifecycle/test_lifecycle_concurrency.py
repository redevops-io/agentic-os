"""Safe-concurrency guard for the lifecycle operator (parallel-execution migration, step 4).
Overlap/conflict assertions through the real TopoScheduler from the operator's DECLARED metadata.
"""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("lifecycle.operator").build_lifecycle_operator()
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


def test_advisory_caps_are_read_only():
    assert _SPECS["lifecycle.segment"].concurrency_mode == "read_only"
    assert _SPECS["lifecycle.suggest_flow"].concurrency_mode == "read_only"
    assert _released(_node("a", "lifecycle.segment"), _node("b", "lifecycle.suggest_flow")) == {"a", "b"}


def test_campaign_drafts_are_bounded_by_the_listmonk_cap():
    ns = [_node(f"c{i}", "lifecycle.compose_campaign") for i in range(3)]
    assert len(_released(*ns)) == 2   # provider:listmonk max_parallelism=2
