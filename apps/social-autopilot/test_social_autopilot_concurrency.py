"""Safe-concurrency guard for the social-autopilot operator (parallel-execution migration, step 4).
Overlap/conflict assertions through the real TopoScheduler from the operator's DECLARED metadata.
"""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("social-autopilot.operator").build_social_operator()
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


def test_publishing_the_same_post_serializes():
    n, reason = _reason(_node("a", "social.publish", id="P1"), _node("b", "social.publish", id="P1"))
    assert n == 1 and "social:post:P1" in reason


def test_publishing_different_posts_parallelizes():
    assert _released(_node("a", "social.publish", id="P1"), _node("b", "social.publish", id="P2")) == {"a", "b"}


def test_drafting_is_bounded_by_the_postiz_provider_cap():
    drafts = [_node(f"d{i}", "social.draft") for i in range(4)]
    assert len(_released(*drafts)) == 3   # max_parallelism=3
