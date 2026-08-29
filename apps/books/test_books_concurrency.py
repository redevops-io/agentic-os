"""Safe-concurrency guard for the books operator (parallel-execution migration, step 4).
Paired overlap/conflict assertions through the real TopoScheduler, from the operator's DECLARED
concurrency metadata. See apps/infra/test_infra_concurrency.py for the template.
"""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("books.operator").build_books_operator()
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


def test_ledger_posts_serialize_on_one_lock():
    n, reason = _serialize_reason(_node("a", "books.categorize"), _node("b", "books.record_revenue"))
    assert n == 1 and "books:ledger" in reason


def test_record_and_reverse_never_overlap():
    assert len(_released(_node("a", "books.record_revenue"), _node("b", "books.reverse_entry"))) == 1


def test_reconcile_is_read_only():
    assert _SPECS["books.reconcile"].concurrency_mode == "read_only"
