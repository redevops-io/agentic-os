"""Runtime interpretation metadata — versions surfaced on EXPLAIN + /buildinfo (replay provenance)."""
from __future__ import annotations

from agentic_os.mission.runtime_meta import runtime_build_metadata


def test_build_metadata_carries_the_interpretation_context():
    m = runtime_build_metadata()
    assert set(m) == {"runtime_contracts_version", "canonicalization_version",
                      "contract_schema_version", "seal_contract_version"}
    # canonicalization is the cross-language anchor; must be the real rcv1 constant, never "unknown"
    assert m["canonicalization_version"] == "rcv1"
    assert m["contract_schema_version"] and m["contract_schema_version"] != "unknown"
    assert isinstance(m["runtime_contracts_version"], str) and m["runtime_contracts_version"]


def test_plan_explain_records_how_it_was_interpreted():
    from agentic_os.mission.types import ExecutionPlan, PlanAxes
    plan = ExecutionPlan.from_axes(PlanAxes(), goal="demo")
    ex = plan.explain()
    assert "interpreted_under" in ex
    assert ex["interpreted_under"]["canonicalization_version"] == "rcv1"
    # a plan's identity is now self-describing: hash + the context it was interpreted under
    assert ex["interpreted_under"] == runtime_build_metadata()
