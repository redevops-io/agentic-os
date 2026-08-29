"""Safe-concurrency guard for the outreach-engine operator (parallel-execution migration, step 4).
Paired overlap/conflict assertions through the real TopoScheduler, from the operator's DECLARED
concurrency metadata. See apps/infra/test_infra_concurrency.py for the template.
"""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("outreach-engine.operator").build_outreach_operator()
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


def test_refresh_is_read_only():
    assert _SPECS["outreach.refresh"].concurrency_mode == "read_only"


def test_approvals_for_different_accounts_parallelize():
    assert _released(_node("a", "outreach.approve", account="A1"),
                     _node("b", "outreach.approve", account="A2")) == {"a", "b"}


def test_same_account_serializes():
    n, reason = _serialize_reason(_node("a", "outreach.approve", account="A1"),
                                  _node("b", "outreach.approve", account="A1"))
    assert n == 1 and "outreach:account:A1" in reason


def test_send_all_is_globally_serial():
    assert len(_released(_node("a", "outreach.send_all"), _node("b", "outreach.send_all"))) == 1
