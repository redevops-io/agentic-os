"""Supplier resolution — internal ERP resolution + GLEIF/open fallback via the broker ladder. Offline.

Pins the Partnership-Strategy routing: internal records first, GLEIF/open next, realised through the
ordinary resolve_decision_need fallback ladder (internal returns LOW_CONFIDENCE → falls through to open).
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.supply import Supplier
from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    resolve_supplier, supplier_resolution_registry, supplier_resolution_synthesize,
)
from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, DecisionNeed, EvidenceArtifact,
    ProviderFamily,
)


def _sup(ref, name, **ext):
    return Supplier(prov=Provenance(provider="erp", provider_ref=ref), name=name, external_ids=ext)


SUPPLIERS = [
    _sup("sup:acme", "Acme Industrial GmbH", lei="ACME123", domain="acme.example", tax_id="DE999"),
    _sup("sup:beta", "Beta Components Ltd", lei="BETA456"),
]


@dataclass
class FakeGleif:
    """A COMPANY_IDENTITY provider returning a GLEIF-shaped record."""
    provider_id: str = "gleif"
    family: ProviderFamily = ProviderFamily.EXTERNAL_DATA
    result: str = "found"
    calls: int = 0

    def capabilities(self): return (Capability.COMPANY_IDENTITY,)
    def estimate_cost(self, r): return CostEstimate(money=0.0, latency_ms=10)
    def check_entitlement(self, t, c): return True

    def acquire(self, request):
        self.calls += 1
        if self.result != "found":
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no LEI")
        name = request.subject_refs[0]
        art = EvidenceArtifact(
            provider="gleif", family=self.family, capability=Capability.COMPANY_IDENTITY, subject=name,
            observations=({"lei": "GLEIF-LEI-1", "legal_name": "Gamma Corporation", "status": "ACTIVE"},),
            retrieved_at="2026-09-26T00:00:00Z", confidence=0.95, cost=0.0, license_scope="GLEIF (CC0)",
            raw_response_digest="x")
        return AcquisitionResult.found((art,))


# ── internal resolution ─────────────────────────────────────────────────────────────────────────────────
def test_exact_lei_match():
    r = resolve_supplier("ACME123", SUPPLIERS)
    assert r.supplier_ref == "sup:acme" and r.matched_on == "lei" and r.confidence == 0.99


def test_domain_and_exact_name_and_fuzzy():
    assert resolve_supplier("acme.example", SUPPLIERS).matched_on == "domain"
    assert resolve_supplier("Acme Industrial GmbH", SUPPLIERS).matched_on == "name_exact"
    fuzzy = resolve_supplier("Acme Industrial", SUPPLIERS)     # 2 of 3 tokens
    assert fuzzy.matched_on == "name_fuzzy" and fuzzy.confidence == round(0.5 + 0.35 * (2 / 3), 4)


def test_no_match_returns_none():
    assert resolve_supplier("Wingtip Toys", SUPPLIERS) is None


# ── broker routing: internal first, open next ───────────────────────────────────────────────────────────
def _need(query, **kw):
    base = dict(decision_case_id="dc1", capability=Capability.SUPPLIER_RESOLUTION, question="who is this?",
                objective="onboard_supplier", subject_refs=(query,), tenant="t", min_confidence=0.0)
    base.update(kw)
    return DecisionNeed(**base)


def test_confident_internal_match_does_not_touch_gleif():
    gleif = FakeGleif()
    reg = supplier_resolution_registry(SUPPLIERS, identity=gleif)
    res, _ = resolve_decision_need(reg, _need("ACME123"), synthesize=supplier_resolution_synthesize)
    assert res.metrics["tier"] == "internal" and res.metrics["supplier_ref"] == "sup:acme"
    assert res.provider_receipts[0].provider == "internal.supplier_resolution"
    assert gleif.calls == 0                                    # open source never consulted


def test_low_confidence_internal_falls_through_to_open_gleif():
    gleif = FakeGleif()
    reg = supplier_resolution_registry(SUPPLIERS, identity=gleif)
    # 'Gamma Corp' has no ERP record → internal LOW_CONFIDENCE → ladder falls through to GLEIF.
    res, trace = resolve_decision_need(reg, _need("Gamma Corp"), synthesize=supplier_resolution_synthesize)
    assert res.metrics["tier"] == "open" and res.metrics["lei"] == "GLEIF-LEI-1"
    assert "Gamma Corporation" in res.answer
    assert [r.provider for r in res.provider_receipts] == ["internal.supplier_resolution",
                                                           "open.gleif_supplier_resolution"]
    assert gleif.calls == 1


def test_fuzzy_below_threshold_also_escalates():
    gleif = FakeGleif()
    reg = supplier_resolution_registry(SUPPLIERS, identity=gleif)
    # 'Acme Industrial' fuzzy-matches at ~0.733 < 0.75 internal threshold → escalate to open.
    res, _ = resolve_decision_need(reg, _need("Acme Industrial"), synthesize=supplier_resolution_synthesize)
    assert res.metrics["tier"] == "open" and gleif.calls == 1


def test_no_open_provider_and_no_internal_match_is_unresolved():
    reg = supplier_resolution_registry(SUPPLIERS)              # internal only, no GLEIF
    res, _ = resolve_decision_need(reg, _need("Gamma Corp"), synthesize=supplier_resolution_synthesize)
    assert res.answer == "" and res.unresolved_gaps == ("no evidence acquired",)
