"""Asset Intelligence — identity / history / failure / maintenance / parts / replacement + broker. Offline."""
from __future__ import annotations

from agentic_os.integrations.business.asset import Asset, Component, ServiceEvent, WorkOrder
from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.supply import InventoryPosition
from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    AssetGraph, AssetIntelligenceProvider, asset_identity, asset_registry, asset_synthesize, failure_risk,
    maintenance_risk, parts_risk, replacement_compatibility, service_history,
)
from runtime_contracts.protocol import Capability, DecisionNeed, ProviderFamily

A = "asset:pump1"


def _pr(ref):
    return Provenance(provider="cmms", provider_ref=ref)


def _graph():
    return AssetGraph(
        assets=[Asset(prov=_pr(A), model="X200", serial="SN1", oem="Acme", site="P1", status="operating"),
                Asset(prov=_pr("asset:pump2"), model="X200", serial="SN2", oem="Acme")],
        components=[Component(prov=_pr("c1"), asset_ref=A, part="seal", serial="s1", position="P-seal"),
                    Component(prov=_pr("c2"), asset_ref=A, part="impeller", serial="i1", position="P-imp"),
                    Component(prov=_pr("c3"), asset_ref="asset:pump2", part="seal-alt", serial="s3",
                              position="P-seal")],
        work_orders=[WorkOrder(prov=_pr("wo1"), asset_ref=A, kind="maintenance", scheduled_date="2026-03-01",
                               status="planned", part_refs=("seal",)),
                     WorkOrder(prov=_pr("wo2"), asset_ref=A, kind="repair", scheduled_date="2026-03-05",
                               status="planned", part_refs=("gasket",))],
        service_events=[ServiceEvent(prov=_pr("e1"), asset_ref=A, at="2026-01-01T00:00:00Z", kind="install"),
                        ServiceEvent(prov=_pr("e2"), asset_ref=A, at="2026-01-31T00:00:00Z", kind="failure",
                                     downtime_hours=6.0),
                        ServiceEvent(prov=_pr("e3"), asset_ref=A, at="2026-03-02T00:00:00Z", kind="repair",
                                     downtime_hours=4.0)],
        inventory=[InventoryPosition(prov=_pr("inv-seal"), part="seal", site="P1", on_hand=3.0)],
    )


# ── deterministic ───────────────────────────────────────────────────────────────────────────────────────
def test_asset_identity_and_component_tree():
    idn = asset_identity(A, _graph().assets, _graph().components)
    assert idn.oem == "Acme" and idn.model == "X200"
    assert ("P-imp", "impeller", "i1") in idn.components and len(idn.components) == 2


def test_service_history_is_chronological_with_downtime():
    h = service_history(A, _graph().service_events)
    assert [e[1] for e in h.events] == ["install", "failure", "repair"]
    assert h.failure_count == 1 and h.total_downtime_hours == 10.0


def test_failure_risk_is_a_forecast():
    fr = failure_risk(A, _graph().service_events, horizon_days=60)
    assert fr.failures == 1 and fr.observed_days > 0 and 0.0 < fr.p_failure_in_horizon < 1.0


def test_maintenance_risk_blocks_on_missing_parts():
    g = _graph()
    ok = maintenance_risk("wo1", g.work_orders, g.inventory)     # needs 'seal', in stock
    blocked = maintenance_risk("wo2", g.work_orders, g.inventory)  # needs 'gasket', not stocked
    assert ok.p_on_time == 1.0 and ok.blocking_parts == ()
    assert blocked.p_on_time == 0.0 and blocked.blocking_parts == ("gasket",)


def test_parts_risk_cover():
    g = _graph()
    pr = parts_risk("seal", g.components, g.service_events, g.inventory)
    assert pr.installed_base == 1 and pr.on_hand_spares == 3.0


def test_replacement_compatibility_observed_in_same_position():
    opts = replacement_compatibility("seal", _graph().components)
    assert [o.part for o in opts] == ["seal-alt"]               # seen in the same P-seal position elsewhere


# ── broker path ─────────────────────────────────────────────────────────────────────────────────────────
def _need(cap, subject_refs):
    return DecisionNeed(decision_case_id="dc1", capability=cap, question="?", objective="asset_review",
                        subject_refs=subject_refs, tenant="t", min_confidence=0.0, as_of="2026-03-03T00:00:00Z",
                        known_at="2026-03-03T00:00:00Z")


def test_asset_capabilities_resolve_through_the_broker():
    reg = asset_registry(_graph())
    idn, _ = resolve_decision_need(reg, _need(Capability.ASSET_IDENTITY, ("asset:pump1",)), synthesize=asset_synthesize)
    assert "Acme X200" in idn.answer and idn.provider_receipts[0].provider == "internal.asset_intelligence"
    mr, _ = resolve_decision_need(reg, _need(Capability.MAINTENANCE_RISK, ("work=wo2",)), synthesize=asset_synthesize)
    assert "BLOCKED on parts" in mr.answer and mr.metrics["p_on_time"] == 0.0
    fr, _ = resolve_decision_need(reg, _need(Capability.FAILURE_RISK, ("asset=asset:pump1", "horizon_days=60")),
                                  synthesize=asset_synthesize)
    assert "P(failure" in fr.answer and "Forecast" in fr.answer


def test_provider_is_cost_zero_and_internal_family():
    p = AssetIntelligenceProvider(_graph())
    assert p.family is ProviderFamily.INTERNAL_COMPUTED
    assert p.estimate_cost(_need(Capability.ASSET_IDENTITY, ("a",)).to_evidence_request()).money == 0.0
