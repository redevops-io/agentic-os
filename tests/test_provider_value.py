"""Provider-value learning + cost-aware routing over the value ledger (Phase 6). Offline."""
from __future__ import annotations

from agentic_os.intelligence import (
    EvidenceValueStore, learn_priors, resolve_decision_need, routing_report, value_fn_from_store,
)
from runtime_contracts.protocol import (
    AcquisitionResult, Capability, CostEstimate, DecisionNeed, EvidenceArtifact, EvidenceRef,
    EvidenceValueRecord, IntelligenceRegistry, ProviderFamily,
)


def _rec(provider, cap, *, changed, beneficial, cost=0.2):
    """A ledger row: changed decision (before≠after) and, if beneficial, a beneficial verified outcome."""
    return EvidenceValueRecord(
        decision_case_id="dc", capability=cap, provider=provider, evidence_requested=True,
        evidence_received=True, cost=cost, decision_before="A", decision_after=("B" if changed else "A"),
        verified_outcome=("beneficial" if beneficial else "neutral"))


def _store(tmp_path, rows):
    s = EvidenceValueStore(str(tmp_path / "v.jsonl"))
    for r in rows:
        s.append(r)
    return s


def test_priors_recommend_stop_for_a_provider_that_never_helps(tmp_path):
    rows = ([_rec("good", Capability.COMPANY_IDENTITY, changed=True, beneficial=True) for _ in range(6)]
            + [_rec("dud", Capability.COMPANY_IDENTITY, changed=False, beneficial=False) for _ in range(6)])
    priors = learn_priors(_store(tmp_path, rows))
    good = priors[("good", "company_identity")]
    dud = priors[("dud", "company_identity")]
    assert good.recommend == "buy" and good.beneficial_rate == 1.0
    assert dud.recommend == "stop" and dud.beneficial_rate == 0.0
    assert good.value_estimate > dud.value_estimate


def test_trial_when_too_little_history(tmp_path):
    rows = [_rec("new", Capability.SANCTIONS_RISK, changed=True, beneficial=True) for _ in range(2)]
    priors = learn_priors(_store(tmp_path, rows), min_lookups=5)
    assert priors[("new", "sanctions_risk")].recommend == "trial"


def test_routing_report_orders_stops_first(tmp_path):
    rows = ([_rec("good", Capability.COMPANY_IDENTITY, changed=True, beneficial=True) for _ in range(6)]
            + [_rec("dud", Capability.COMPANY_IDENTITY, changed=False, beneficial=False) for _ in range(6)])
    report = routing_report(_store(tmp_path, rows))
    assert report[0].recommend == "stop" and report[0].provider == "dud"


# ── the learned value_fn gates a low-value capability in the resolver ─────────────────────────────────────
class _PaidProvider:
    provider_id = "paid"
    family = ProviderFamily.EXTERNAL_DATA

    def __init__(self):
        self.calls = 0

    def capabilities(self): return (Capability.COMPANY_IDENTITY,)
    def estimate_cost(self, r): return CostEstimate(money=0.5, latency_ms=10)
    def check_entitlement(self, t, c): return True

    def acquire(self, request):
        self.calls += 1
        art = EvidenceArtifact(provider="paid", family=self.family, capability=request.capability,
                               subject="x", observations=({"v": 1},), retrieved_at="2026-01-01T00:00:00Z",
                               confidence=0.9, cost=0.5, license_scope="paid", raw_response_digest="d")
        return AcquisitionResult.found((art,), cost=0.5)


def _need(cap=Capability.COMPANY_IDENTITY):
    return DecisionNeed(decision_case_id="dc2", capability=cap, subject_refs=("acme",), tenant="t",
                        max_cost=10.0, min_confidence=0.0)


def test_learned_low_value_capability_is_not_bought(tmp_path):
    # history: COMPANY_IDENTITY has repeatedly failed to change/benefit decisions → learned value ~0.
    rows = [_rec("paid", Capability.COMPANY_IDENTITY, changed=False, beneficial=False, cost=0.5) for _ in range(8)]
    vfn = value_fn_from_store(_store(tmp_path, rows))
    provider = _PaidProvider()
    reg = IntelligenceRegistry(); reg.register(provider)
    res, trace = resolve_decision_need(reg, _need(), value_fn=vfn)
    assert provider.calls == 0                       # gate skipped the low-value paid call
    assert res.total_cost == 0.0 and res.provider_receipts == ()


def test_unlearned_capability_still_explores(tmp_path):
    # no history for this capability → default value 1.0 → the call proceeds (keep learning).
    vfn = value_fn_from_store(_store(tmp_path, []))
    provider = _PaidProvider()
    reg = IntelligenceRegistry(); reg.register(provider)
    res, _ = resolve_decision_need(reg, _need(), value_fn=vfn)
    assert provider.calls == 1 and res.total_cost == 0.5
