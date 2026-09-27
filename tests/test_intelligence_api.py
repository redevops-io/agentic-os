"""Per-app profiles + the §3 FastAPI surface over IntelligenceService. Offline (in-process TestClient)."""
from __future__ import annotations

import pytest

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.supply import InventoryPosition, PurchaseOrder
from agentic_os.intelligence import IntelligenceService, build_router
from agentic_os.intelligence.apps import app_capabilities, request_for
from agentic_os.intelligence.families import SupplyGraph, supply_registry, supply_synthesize
from runtime_contracts.protocol import Capability


# ── per-app profiles ────────────────────────────────────────────────────────────────────────────────────
def test_erpnext_is_entitled_to_the_operational_capabilities():
    caps = app_capabilities("erpnext")
    for c in (Capability.SUPPLIER_RESOLUTION, Capability.SHORTAGE_RISK, Capability.ORDER_LINEAGE,
              Capability.PROMISE_FEASIBILITY, Capability.PAYMENT_BEHAVIOR, Capability.WHY_STUCK,
              Capability.ASSET_IDENTITY, Capability.FAILURE_RISK):
        assert c in caps


def test_request_for_tags_purpose_from_the_profile():
    req = request_for("erpnext", Capability.DELIVERY_RELIABILITY, decision_case_id="dc",
                      subject_refs=("sup:acme",), tenant="t")
    assert req.capability is Capability.DELIVERY_RELIABILITY
    assert req.purpose == "supplier OTIF assessment before award"


def test_app_cannot_request_capability_outside_remit():
    with pytest.raises(ValueError):
        request_for("umami", Capability.SHORTAGE_RISK, decision_case_id="dc", subject_refs=("x",), tenant="t")


# ── FastAPI surface ─────────────────────────────────────────────────────────────────────────────────────
def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    def _pr(r):
        return Provenance(provider="erp", provider_ref=r)

    graph = SupplyGraph(
        inventory=[InventoryPosition(prov=_pr("i"), part="Rim", site="A", on_hand=20.0)],
        open_supply=[PurchaseOrder(prov=_pr("po1"), supplier_ref="s", site="A", part="Rim", quantity=50.0,
                                   promised_date="2026-02-15", ordered_at="2026-01-01T00:00:00Z")],
        demand=[])
    service = IntelligenceService().bind(supply_registry(graph), supply_synthesize)
    app = FastAPI()
    app.include_router(build_router(service))
    return TestClient(app)


def test_capabilities_endpoint_lists_served_capabilities():
    r = _client().get("/v1/intelligence/capabilities")
    assert r.status_code == 200 and "shortage_risk" in r.json()["capabilities"]


def test_resolve_endpoint_returns_a_result_and_get_replays_it():
    c = _client()
    r = c.post("/v1/intelligence/supply/shortage_risk",
               json={"subject_refs": ["Rim", "A"], "tenant": "t"})
    assert r.status_code == 200
    body = r.json()
    assert body["capability"] == "shortage_risk" and "request_id" in body and body["total_cost"] == 0.0
    # GET the stored result by its request_id (replay).
    got = c.get(f"/v1/intelligence/requests/{body['request_id']}")
    assert got.status_code == 200 and got.json()["request_id"] == body["request_id"]


def test_quote_endpoint_estimates_before_execution():
    r = _client().post("/v1/intelligence/quote",
                       json={"capability": "shortage_risk", "subject_refs": ["Rim", "A"], "tenant": "t"})
    assert r.status_code == 200 and r.json()["available"] is True
    assert r.json()["expected_cost"] == 0.0


def test_unknown_capability_is_400():
    r = _client().post("/v1/intelligence/supply/not_a_capability", json={"subject_refs": ["x"], "tenant": "t"})
    assert r.status_code == 400


def test_missing_request_is_404():
    r = _client().get("/v1/intelligence/requests/does-not-exist")
    assert r.status_code == 404
