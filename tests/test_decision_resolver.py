"""DecisionNeed → IntelligenceResult resolution (Intelligence-APIs plan §2/§3). All offline.

Pins the guarantees the decision-scoped broker layer relies on: a need resolves to a governed result with a
per-call provider receipt ledger and a total spend derived from it; the fallback ladder is honoured; the
need's policy envelope (permitted providers, min confidence) and provider health route acquisition; budget
stops the ladder without a silent weaker substitute; and the value ledger records the resolution.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, DecisionNeed, EvidenceArtifact,
    EvidenceRef, HealthStatus, IntelligenceRegistry, ProviderFamily, ProviderHealth,
)

from agentic_os.intelligence import EvidenceValueStore, resolve_decision_need


@dataclass
class FakeProvider:
    provider_id: str
    price: float = 0.4
    caps: tuple = (Capability.COMPANY_IDENTITY,)
    entitled: tuple = ("t",)
    result: str = "found"                 # found | no_match | unavailable
    conf: float = 0.9
    health_status: HealthStatus | None = None   # None ⇒ no health() implemented
    calls: list = field(default_factory=list)

    def capabilities(self): return self.caps
    def estimate_cost(self, request): return CostEstimate(money=self.price, latency_ms=5)
    def check_entitlement(self, tenant, capability): return tenant in self.entitled

    def acquire(self, request):
        self.calls.append(request)
        if self.result == "found":
            art = EvidenceArtifact(
                provider=self.provider_id, family=ProviderFamily.EXTERNAL_DATA, capability=request.capability,
                subject=(request.subject_refs or ("?",))[0],
                observations=({"field": "lei", "value": "5493..."},),
                source_refs=(EvidenceRef(ref=f"{self.provider_id}:rec", content_hash="abc",
                                         source=self.provider_id),),
                retrieved_at="2026-09-26T00:00:00Z", freshness_s=100, confidence=self.conf,
                cost=self.price, license_scope="identity", raw_response_digest="deadbeef")
            return AcquisitionResult.found((art,), cost=self.price)
        if self.result == "no_match":
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no record", cost=0.0)
        return AcquisitionResult.failed(AcquisitionFailure.UNAVAILABLE, "down", cost=0.0)


@dataclass
class HealthyProvider(FakeProvider):
    def health(self) -> ProviderHealth:
        return ProviderHealth(self.provider_id, self.health_status or HealthStatus.OK)


def _need(**kw) -> DecisionNeed:
    base = dict(decision_case_id="dc1", capability=Capability.COMPANY_IDENTITY, question="who is ACME?",
                objective="onboard_supplier", subject_refs=("acme.example",), tenant="t", max_cost=5.0,
                as_of="2026-09-26T00:00:00Z", known_at="2026-09-26T00:00:00Z")
    base.update(kw)
    return DecisionNeed(**base)


def _registry(*providers) -> IntelligenceRegistry:
    r = IntelligenceRegistry()
    for p in providers:
        r.register(p)
    return r


def test_resolves_to_result_with_receipt_ledger_and_carried_anchor():
    reg = _registry(FakeProvider("gleif", price=0.4))
    res, trace = resolve_decision_need(reg, _need())
    assert res.decision_need_id == _need().identity()
    assert len(res.provider_receipts) == 1 and res.provider_receipts[0].provider == "gleif"
    assert res.provider_receipts[0].cost == 0.4 and res.total_cost == 0.4      # derived from receipts
    assert len(res.evidence_event_ids) == 1
    assert res.tenant == "t" and res.as_of == "2026-09-26T00:00:00Z" and res.known_at == "2026-09-26T00:00:00Z"
    assert res.confidence == 0.9 and res.is_decision_grade(_need())


def test_fallback_ladder_receipts_both_attempts_and_sums_cost():
    # cheapest first: a 0.1 provider that NO_MATCHes, then a 0.4 provider that succeeds.
    miss = FakeProvider("cheap", price=0.1, result="no_match")
    hit = FakeProvider("gleif", price=0.4, result="found")
    res, trace = resolve_decision_need(_registry(hit, miss), _need())
    provs = [r.provider for r in res.provider_receipts]
    assert provs == ["cheap", "gleif"]                       # tried cheap first, then fell through
    assert not res.provider_receipts[0].ok and res.provider_receipts[1].ok
    assert res.total_cost == 0.4                             # failed attempt cost 0.0 + success 0.4


def test_unhealthy_provider_is_routed_around():
    down = HealthyProvider("down", price=0.1, health_status=HealthStatus.UNAVAILABLE)
    up = FakeProvider("gleif", price=0.4, result="found")
    res, trace = resolve_decision_need(_registry(down, up), _need())
    assert [r.provider for r in res.provider_receipts] == ["gleif"]   # 'down' never called
    assert not down.calls
    assert any("down: excluded (health UNAVAILABLE" in t for t in trace)


def test_permitted_providers_envelope_excludes_others():
    a = FakeProvider("apollo", price=0.1, result="found")
    g = FakeProvider("gleif", price=0.4, result="found")
    res, _ = resolve_decision_need(_registry(a, g), _need(permitted_providers=("gleif",)))
    assert [r.provider for r in res.provider_receipts] == ["gleif"]   # apollo not permitted, skipped
    assert not a.calls


def test_budget_gate_stops_the_ladder_without_spend():
    pricey = FakeProvider("pricey", price=9.0, result="found")
    res, trace = resolve_decision_need(_registry(pricey), _need(max_cost=1.0))
    assert res.provider_receipts == () and res.total_cost == 0.0
    assert not pricey.calls                                  # gated before any call
    assert res.unresolved_gaps == ("no evidence acquired",) and res.confidence == 0.0


def test_min_confidence_records_a_gap():
    weak = FakeProvider("gleif", price=0.4, result="found", conf=0.6)
    res, _ = resolve_decision_need(_registry(weak), _need(min_confidence=0.9))
    assert "confidence below min_confidence" in res.unresolved_gaps
    assert not res.is_decision_grade(_need(min_confidence=0.9))


def test_value_ledger_records_the_resolution(tmp_path):
    store = EvidenceValueStore(str(tmp_path / "value.jsonl"))
    resolve_decision_need(_registry(FakeProvider("gleif", price=0.4)), _need(), store=store)
    recs = store.records()
    assert len(recs) == 1 and recs[0].provider == "gleif" and recs[0].evidence_received
    assert recs[0].cost == 0.4


def test_ttl_sets_expiry():
    res, _ = resolve_decision_need(_registry(FakeProvider("gleif")), _need(),
                                   produced_at="2026-09-26T00:00:00Z", ttl_s=3600)
    assert res.expires_at == "2026-09-26T01:00:00Z"
    assert res.is_expired("2026-09-26T02:00:00Z") and not res.is_expired("2026-09-26T00:30:00Z")
