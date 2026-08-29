"""Safe-concurrency guard for the billing operator (parallel-execution migration, step 4).
Paired overlap/conflict assertions through the real TopoScheduler, from the operator's DECLARED
concurrency metadata. See apps/infra/test_infra_concurrency.py for the template.
"""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("billing.operator").build_billing_operator()
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


def test_summary_is_read_only():
    assert _SPECS["billing.summary"].concurrency_mode == "read_only"
    assert _released(_node("s1", "billing.summary"), _node("s2", "billing.summary")) == {"s1", "s2"}


def test_subscriptions_for_different_customers_parallelize():
    assert _released(_node("a", "billing.create_subscription", customer="C1"),
                     _node("b", "billing.create_subscription", customer="C2")) == {"a", "b"}


def test_create_and_cancel_same_customer_serialize():
    n, reason = _serialize_reason(_node("a", "billing.create_subscription", customer="C1"),
                                  _node("b", "billing.cancel_subscription", customer="C1"))
    assert n == 1 and "billing:customer:C1" in reason


def test_dunning_runs_are_globally_serial():
    n, reason = _serialize_reason(_node("a", "billing.dunning"), _node("b", "billing.dunning"))
    assert n == 1 and "billing:dunning" in reason
