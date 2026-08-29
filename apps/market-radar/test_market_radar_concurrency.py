"""Safe-concurrency guard for the market-radar operator (parallel-execution migration, step 4).
Paired overlap/conflict assertions through the real TopoScheduler, from the operator's DECLARED
concurrency metadata — not timing. See apps/infra/test_infra_concurrency.py for the template.
"""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("market-radar.operator").build_market_radar_operator()
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


def test_watches_to_different_urls_parallelize():
    a = _node("a", "radar.add_watch", url="http://x.com/a")
    b = _node("b", "radar.add_watch", url="http://y.com/b")
    assert _released(a, b) == {"a", "b"}


def test_watches_to_the_same_url_serialize():
    a = _node("a", "radar.add_watch", url="http://x.com/a")
    b = _node("b", "radar.add_watch", url="http://x.com/a")
    g = ExecutionGraph(nodes=[a, b])
    assert len(_S.ready(g, set(), set(), _W)) == 1
    ser = next(r for r in _S.explain(g, set(), set(), _W) if r["decision"] == "serialized")
    assert "market-radar:watch:http://x.com/a" in ser["reason"]


def test_brief_is_read_only():
    assert _SPECS["radar.brief"].concurrency_mode == "read_only"
    assert _released(_node("b1", "radar.brief"), _node("b2", "radar.brief")) == {"b1", "b2"}
