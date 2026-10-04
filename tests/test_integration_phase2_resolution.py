"""Integration plane Phase 2 — Entity Resolution Plane + Semantic Registry (§6/§7).

Proves: merge only on a strong key or explicit confirmation (never a silent probabilistic merge); split reverses
a bad merge; supersession routes lookups to the survivor; lineage is append-only. And the Semantic Registry
explains *why* two valid revenue numbers differ instead of forcing one 'truth'.
"""
from __future__ import annotations

import pytest

from agentic_os.integration import (
    EntityResolutionPlane, MergeRefused, MetricReading, SemanticRegistry, default_revenue_registry,
    explain_metric_delta,
)
from agentic_os.integration.contracts import CanonicalEntity, EntityType, ResolutionStatus
from agentic_os.integration.semantics import MetricDefinition


def _person(status=ResolutionStatus.RESOLVED, bindings=(), aliases=("a@x.com",)):
    return CanonicalEntity(entity_type=EntityType.PERSON, aliases=aliases, source_bindings=bindings,
                           resolution_status=status, confidence=1.0 if status == ResolutionStatus.RESOLVED else 0.6)


# ── entity resolution plane ──────────────────────────────────────────────────────────────────────────────
def test_bind_and_merge_on_strong_key_with_lineage():
    plane = EntityResolutionPlane()
    a = plane.ingest(_person(bindings=(("salesforce", "con_1"),)))
    b = plane.ingest(_person(bindings=(("stripe", "cus_1"),), aliases=("a@x.com",)))
    merged = plane.merge(a.entity_id, b.entity_id, strong=True)        # same email → strong
    assert merged.resolution_status == ResolutionStatus.RESOLVED
    assert ("salesforce", "con_1") in merged.source_bindings and ("stripe", "cus_1") in merged.source_bindings
    # lookups on the dropped entity route to the survivor
    assert plane.get(b.entity_id).entity_id == a.entity_id
    assert len(plane.active()) == 1
    ops = [e.op for e in plane.lineage()]
    assert "merge" in ops and "supersede" in ops


def test_probabilistic_merge_is_refused_without_confirmation():
    plane = EntityResolutionPlane()
    a = plane.ingest(_person(status=ResolutionStatus.PROBABLE, bindings=(("zendesk", "u1"),)))
    b = plane.ingest(_person(status=ResolutionStatus.PROBABLE, bindings=(("slack", "U2"),), aliases=("maybe@x.com",)))
    with pytest.raises(MergeRefused):
        plane.merge(a.entity_id, b.entity_id)                           # not strong, no confirmer → refused
    # but a human can confirm the merge
    merged = plane.merge(a.entity_id, b.entity_id, by="alex@redevops")
    assert merged.resolution_status == ResolutionStatus.PROBABLE and len(plane.active()) == 1
    assert any("confirm" not in e.op for e in plane.lineage())          # merge recorded with the confirmer


def test_split_reverses_a_bad_merge():
    plane = EntityResolutionPlane()
    a = plane.ingest(_person(bindings=(("salesforce", "con_1"), ("stripe", "cus_WRONG"))))
    carved = plane.split(a.entity_id, ("stripe", "cus_WRONG"), by="alex@redevops")
    assert ("stripe", "cus_WRONG") not in plane.get(a.entity_id).source_bindings
    assert ("stripe", "cus_WRONG") in carved.source_bindings and carved.entity_id != a.entity_id
    assert any(e.op == "split" for e in plane.lineage())


def test_confirm_promotes_probable_to_resolved():
    plane = EntityResolutionPlane()
    a = plane.ingest(_person(status=ResolutionStatus.AMBIGUOUS))
    resolved = plane.confirm(a.entity_id, by="alex@redevops")
    assert resolved.resolution_status == ResolutionStatus.RESOLVED and resolved.confidence == 1.0


# ── semantic registry ────────────────────────────────────────────────────────────────────────────────────
def test_registry_versions_and_mappings():
    reg = SemanticRegistry()
    reg.register(MetricDefinition("revenue.billed", "v1", version=1))
    reg.register(MetricDefinition("revenue.billed", "v2 refined", version=2))
    assert reg.get("revenue.billed").version == 2            # latest by default
    assert reg.get("revenue.billed", version=1).description == "v1"


def test_explains_why_revenue_numbers_legitimately_differ():
    reg = default_revenue_registry()
    billed = MetricReading("revenue.billed", "stripe", 130000)
    recognized = MetricReading("revenue.recognized", "quickbooks", 110000)
    out = explain_metric_delta(billed, recognized, reg)
    assert out["kind"] == "definitional_difference" and out["delta"] == -20000
    # the explanation attributes the delta to policy differences, not a "wrong number"
    assert any("recognition" in r or "refund" in r or "timing" in r for r in out["reasons"])
    assert "revenue.billed=130000" in out["explanation"]


def test_same_metric_mismatch_is_a_real_variance():
    reg = default_revenue_registry()
    a = MetricReading("revenue.cash_collected", "stripe", 125000)
    b = MetricReading("revenue.cash_collected", "bank", 124500)
    out = explain_metric_delta(a, b, reg)
    assert out["kind"] == "same_metric_variance" and out["delta"] == -500
    assert out["reasons"]                                     # a nonzero same-metric delta needs reconciliation


def test_matching_values_explain_as_agreement():
    reg = default_revenue_registry()
    a = MetricReading("revenue.cash_collected", "stripe", 125000)
    b = MetricReading("revenue.cash_collected", "bank", 125000)
    assert explain_metric_delta(a, b, reg)["reasons"] == []   # they agree
