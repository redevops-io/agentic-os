"""Safe-concurrency guard for the zoning operator (parallel-execution migration, step 4).
Paired overlap/conflict assertions through the real TopoScheduler, from the operator's DECLARED
concurrency metadata — not timing. See apps/infra/test_infra_concurrency.py for the template.
"""
from __future__ import annotations

import importlib

import pytest

from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.types import ExecutionGraph, Node

pytest.importorskip("runtime_contracts")   # zoning.core needs it; skip cleanly when absent

_op = importlib.import_module("zoning.operator").build_zoning_operator()
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


def test_all_read_only_caps_run_concurrently():
    caps = list(_SPECS)
    nodes = [_node(f"n{i}", c) for i, c in enumerate(caps) if _SPECS[c].concurrency_mode == "read_only"]
    assert nodes, "expected read-only capabilities"
    assert len(_released(*nodes)) == len(nodes), "read-only caps must not block each other"


def test_read_only_caps_declared():
    ro = [n for n, s in _SPECS.items() if s.concurrency_mode == "read_only"]
    assert ro == ["zoning.resolve_parcel", "zoning.acquire_evidence", "zoning.evaluate_use", "zoning.search_parcels"]
