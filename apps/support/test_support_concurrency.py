"""Safe-concurrency guard for the support operator (parallel-execution migration, step 4).
Overlap/conflict assertions through the real TopoScheduler from the operator's DECLARED metadata.
"""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("support.operator").build_support_operator()
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


def test_actions_on_the_same_conversation_serialize():
    n, reason = _reason(_node("a", "support.draft_reply", conversation_id="C1"),
                        _node("b", "support.resolve", conversation_id="C1"))
    assert n == 1 and "support:conversation:C1" in reason


def test_actions_on_different_conversations_parallelize():
    assert _released(_node("a", "support.resolve", conversation_id="C1"),
                     _node("b", "support.escalate", conversation_id="C2")) == {"a", "b"}


def test_onboarding_is_bounded_by_the_chatwoot_cap():
    ns = [_node(f"o{i}", "support.send_onboarding") for i in range(4)]
    assert len(_released(*ns)) == 3
