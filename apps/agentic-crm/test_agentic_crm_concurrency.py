"""Safe-concurrency guard for the agentic-crm operator (parallel-execution migration, step 4).
Paired overlap/conflict assertions through the real TopoScheduler, from the operator's DECLARED
concurrency metadata. See apps/infra/test_infra_concurrency.py for the template.
"""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("agentic-crm.operator").build_crm_operator()
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


def _serialize_reason(*ns):
    g = ExecutionGraph(nodes=list(ns))
    n_released = len(_S.ready(g, set(), set(), _W))
    ser = [r for r in _S.explain(g, set(), set(), _W) if r["decision"] == "serialized"]
    return n_released, (ser[0]["reason"] if ser else "")


def test_writes_to_different_leads_parallelize():
    assert _released(_node("a", "crm.score_lead", lead="L1"),
                     _node("b", "crm.qualify", lead="L2")) == {"a", "b"}


def test_writes_to_the_same_lead_serialize():
    n, reason = _serialize_reason(_node("a", "crm.score_lead", lead="L1"),
                                  _node("b", "crm.qualify", lead="L1"))
    assert n == 1 and "crm:lead:L1" in reason
