"""Reference ASGI server for the Intelligence §3 API — the deployable entry point for a live demo.

Run it directly:  `uvicorn agentic_os.intelligence.demo_server:app --port 8814`
behind a host port + a cloudflared route + a DNS CNAME (e.g. intel.redevops.io).

It wires an :class:`IntelligenceService` over compact demo data for several families (counterparty, supply,
order) so every §3 endpoint is exercisable, and adds one bespoke route — GET /v1/intelligence/kyc/screen —
that demonstrates the temporal ownership graph: the same vendor screens GO before its upstream sanction was
known and NO-GO after (bi-temporal replay). The demo data is self-contained; a real deployment binds the
tenant's own graphs instead.
"""
from __future__ import annotations

from .apps import app_capabilities
from .families import (
    CounterpartyRecords, InMemoryOrderGraph, SupplyGraph, counterparty_registry, counterparty_synthesize,
    order_registry, order_synthesize, supply_registry, supply_synthesize,
)
from .service import IntelligenceService
from .temporal_graph import project_kyc_ownership, screen_ownership

# ── compact demo data (self-contained; a real deploy binds the tenant's own graphs) ──────────────────────
_KYC_VENDORS = {
    "handlowy": {"name": "HANDLOWY-INWESTYCJE", "country": "PL", "kyc": "NO-GO",
                 "sanctioned_owner": "CITIGROUP INC.", "hops_upstream": 6},
    "banca": {"name": "BANCA CENTRO EMILIA", "country": "IT", "kyc": "GO",
              "sanctioned_owner": None, "hops_upstream": 0},
    "abb": {"name": "ABB AG", "country": "DE", "kyc": "ABSTAIN", "sanctioned_owner": None, "hops_upstream": 0},
}
_KYC_SANCTION_KNOWN = "2026-02-01T00:00:00Z"


def build_service() -> IntelligenceService:
    """An IntelligenceService bound with compact demo data for the counterparty / supply / order families."""
    from ..integrations.business.contracts import Invoice, Provenance, Receivable
    from ..integrations.business.supply import InventoryPosition, PurchaseOrder

    def pr(ref):
        return Provenance(provider="demo", provider_ref=ref)

    supply = SupplyGraph(
        inventory=[InventoryPosition(prov=pr("i"), part="Rim", site="A", on_hand=20.0)],
        open_supply=[PurchaseOrder(prov=pr("po1"), supplier_ref="sup:acme", site="A", part="Rim",
                                   quantity=50.0, promised_date="2026-02-15", ordered_at="2026-01-01T00:00:00Z")],
        demand=[])
    order = InMemoryOrderGraph(_pos=[], _sales_orders=[], _lines=[], _receipts=[], _shipments=[])
    counterparty = CounterpartyRecords(
        invoices=[Invoice(prov=pr("inv1"), customer_ref="cust:acme", amount_cents=1000, amount_paid_cents=1000,
                          currency="USD", status="paid")],
        receivables=[Receivable(prov=pr("r1"), customer_ref="cust:acme", amount_outstanding_cents=0,
                                currency="USD", days_overdue=0)])

    svc = IntelligenceService()
    svc.bind(supply_registry(supply), supply_synthesize)
    svc.bind(order_registry(order), order_synthesize)
    svc.bind(counterparty_registry(counterparty), counterparty_synthesize)
    return svc


def build_app():
    """Build the FastAPI app: the §3 router + a KYC temporal-screening demo route + a small index."""
    from fastapi import FastAPI

    from .api import build_router

    service = build_service()
    kyc_graph = project_kyc_ownership(_KYC_VENDORS, sanction_known_at=_KYC_SANCTION_KNOWN)

    app = FastAPI(title="ReDevOps Decision Intelligence", version="1")
    app.include_router(build_router(service))

    @app.get("/")
    def index() -> dict:
        return {
            "service": "ReDevOps Decision Intelligence (§3 API)",
            "capabilities": list(service.capabilities()),
            "erpnext_entitlements": [c.value for c in app_capabilities("erpnext")],
            "endpoints": ["POST /v1/intelligence/{domain}/{capability}", "POST /v1/intelligence/quote",
                          "GET /v1/intelligence/requests/{id}", "GET /v1/intelligence/capabilities",
                          "GET /v1/intelligence/kyc/screen?applicant=&as_of=&known_at="],
        }

    @app.get("/v1/intelligence/kyc/screen")
    def kyc_screen(applicant: str, as_of: str, known_at: str = "") -> dict:
        """Replayable KYC ownership screening over the temporal graph — GO before the upstream sanction's
        known_at, NO-GO after. `known_at` defaults to `as_of`."""
        return screen_ownership(kyc_graph, applicant, valid_time=as_of, known_at=known_at or None)

    return app


app = build_app()
