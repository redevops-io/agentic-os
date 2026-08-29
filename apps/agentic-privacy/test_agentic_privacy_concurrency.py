"""Safe-concurrency guard for the agentic-privacy operator (parallel-execution migration, step 4)."""
from __future__ import annotations

import importlib

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

_op = importlib.import_module("agentic-privacy.operator").build_privacy_operator()
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


def test_writes_on_the_same_subject_serialize():
    n, reason = _reason(_node("a", "privacy.intake", email="x@y.com"),
                        _node("b", "privacy.delete", email="x@y.com"))
    assert n == 1 and "privacy:subject:x@y.com" in reason


def test_writes_on_different_subjects_parallelize():
    assert _released(_node("a", "privacy.intake", email="a@y.com"),
                     _node("b", "privacy.delete", email="b@y.com")) == {"a", "b"}


def test_reads_are_read_only():
    assert _SPECS["privacy.access"].concurrency_mode == "read_only"
    assert _SPECS["privacy.retention"].concurrency_mode == "read_only"
