"""Safe-concurrency guard for the growth-engine operator (parallel-execution migration, step 4).
Paired overlap/conflict assertions through the real TopoScheduler, from the operator's DECLARED
concurrency metadata — not timing. See apps/infra/test_infra_concurrency.py for the template.
"""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("growth-engine.operator").build_growth_operator()
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


def test_budget_reallocations_serialize_on_the_ad_budget():
    a = _node("a", "growth.reallocate_budget")
    b = _node("b", "growth.reallocate_budget")
    g = ExecutionGraph(nodes=[a, b])
    assert len(_S.ready(g, set(), set(), _W)) == 1, "two budget moves must not run at once"
    ser = next(r for r in _S.explain(g, set(), set(), _W) if r["decision"] == "serialized")
    assert "growth:ad-budget" in ser["reason"]


def test_analyze_is_read_only():
    assert _SPECS["growth.analyze"].concurrency_mode == "read_only"
