"""Safe-concurrency guard for the growth-assistant operator (parallel-execution migration, step 4).
Overlap/conflict assertions through the real TopoScheduler from the operator's DECLARED metadata.
"""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("growth-assistant.operator").build_growth_assistant_operator()
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


def test_advisory_caps_are_read_only_and_never_block():
    ro = [n for n, s in _SPECS.items() if s.concurrency_mode == "read_only"]
    assert set(ro) == {"assistant.playbook", "assistant.subreddit_plan", "assistant.hire_brief", "assistant.ask"}
    assert len(_released(*[_node(f"n{i}", c) for i, c in enumerate(ro)])) == len(ro)


def test_pushes_to_different_providers_parallelize():
    assert _released(_node("a", "assistant.founder_content"),
                     _node("b", "assistant.community_blueprint")) == {"a", "b"}


def test_same_provider_pushes_are_bounded():
    ns = [_node(f"p{i}", "assistant.founder_content") for i in range(3)]
    assert len(_released(*ns)) == 2   # provider:postiz max_parallelism=2
