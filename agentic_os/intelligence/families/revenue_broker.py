"""Serve Revenue Intelligence through the broker as an INTERNAL provider (Revenue & Execution plan §6, §8).

The plan's "internal evidence first" rule applies to revenue exactly as it does to supplier / order: quote
feasibility and revenue leakage are computed from the tenant's OWN resolved business state, so they resolve
as a cost-0 `IntelligenceProvider` (family INTERNAL_COMPUTED) through the same `resolve_decision_need` path
as any external provider — receipt ledger, health, value accounting — and a revenue synthesizer turns each
into a business answer. This is what makes `QUOTE_FEASIBILITY` / `REVENUE_LEAKAGE` first-class, routable,
receipted, replayable capabilities (a `DecisionNeed(QUOTE_FEASIBILITY) → IntelligenceResult`), rather than
bare functions an app happens to call.

The two capabilities wrap the domain cores built in `agentic_os.revenue`:
  * QUOTE_FEASIBILITY → `revenue.quote.assess_quote_feasibility` over resolved catalog + customer terms.
  * REVENUE_LEAKAGE   → the `revenue.leakage` detectors (a scan of resolved state for recoverable revenue).

Inputs are resolved facts, not raw provider calls: an `InMemoryRevenueState` is enough for first deployments
and tests; a later slice feeds it from the real Twenty / ERPNext / Chatwoot / Lago clients (the Twenty
`scan_stalled` sensor already produces `RevenueLeakage`s straight into the leakage map).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Protocol

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, DecisionNeed, EvidenceArtifact,
    EvidenceRequest, IntelligenceRegistry, ProviderFamily,
)
from runtime_contracts.protocol.seal import content_hash

from ...revenue.leakage import RevenueLeakage
from ...revenue.quote import CatalogItem, QuoteLine, assess_quote_feasibility

_SERVES = (Capability.QUOTE_FEASIBILITY, Capability.REVENUE_LEAKAGE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _leak_dict(leak: RevenueLeakage) -> dict:
    """A JSON/hash-safe view of a RevenueLeakage (the enum flattened to its value)."""
    return {
        "leakage_type": leak.leakage_type.value, "subject": leak.subject,
        "expected_value": leak.expected_value, "confidence": leak.confidence, "urgency": leak.urgency,
        "proposed_action": leak.proposed_action, "required_capability": leak.required_capability,
        "detail": leak.detail, "amount_cents": leak.amount_cents,
        "observation_refs": list(leak.observation_refs),
    }


# ── quote inputs (the resolved facts a QUOTE_FEASIBILITY need is assessed against) ────────────────────────
@dataclass(frozen=True)
class QuoteInputs:
    """Everything `assess_quote_feasibility` needs for one requested quote, already resolved from the
    operational systems (ERPNext catalog + Twenty customer terms)."""
    lines: List[QuoteLine]
    catalog: Mapping[str, CatalogItem]
    now_ms: int
    currency: str = "USD"
    min_margin_pct: float = 0.2
    approval_over_cents: int = 2_000_000
    customer_discount_pct: float = 0.0


# ── revenue state source ──────────────────────────────────────────────────────────────────────────────────
class RevenueStateSource(Protocol):
    """The tenant's resolved revenue state, keyed by subject ref. A store-backed source can implement this;
    the in-memory one below is enough for first deployments and tests.

    `quote_inputs` / `leakages` return None for an UNKNOWN subject (→ NO_MATCH, try the next provider); a
    KNOWN subject with nothing to report returns its (empty) result, which is a real answer, not a miss."""
    def quote_inputs(self, subject_ref: str) -> Optional[QuoteInputs]: ...
    def leakages(self, subject_ref: str) -> Optional[List[RevenueLeakage]]: ...


@dataclass
class InMemoryRevenueState:
    """In-memory revenue state: a map of subject → requested-quote inputs, and subject → detected leakages
    (e.g. the Twenty `scan_stalled` output, grouped by account/scope)."""
    _quotes: Dict[str, QuoteInputs] = field(default_factory=dict)
    _leakages: Dict[str, List[RevenueLeakage]] = field(default_factory=dict)

    def add_quote(self, subject_ref: str, inputs: QuoteInputs) -> None:
        self._quotes[subject_ref] = inputs

    def set_leakages(self, subject_ref: str, leaks: List[RevenueLeakage]) -> None:
        self._leakages[subject_ref] = list(leaks)

    def quote_inputs(self, subject_ref: str) -> Optional[QuoteInputs]:
        return self._quotes.get(subject_ref)

    def leakages(self, subject_ref: str) -> Optional[List[RevenueLeakage]]:
        return self._leakages.get(subject_ref)


# ── internal provider ────────────────────────────────────────────────────────────────────────────────────
@dataclass
class RevenueIntelligenceProvider:
    """Computes quote feasibility / revenue leakage from the tenant's own resolved state — cost 0, always
    entitled (own data)."""
    source: RevenueStateSource
    provider_id: str = "internal.revenue_intelligence"
    family: ProviderFamily = ProviderFamily.INTERNAL_COMPUTED

    def capabilities(self) -> tuple[Capability, ...]:
        return _SERVES

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.0, latency_ms=1)

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in _SERVES        # the tenant's own data — always entitled

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        subject = request.subject_refs[0] if request.subject_refs else ""
        cap = request.capability
        if cap is Capability.QUOTE_FEASIBILITY:
            qi = self.source.quote_inputs(subject)
            if qi is None:
                return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no requested quote for subject")
            qf = assess_quote_feasibility(
                qi.lines, qi.catalog, now_ms=qi.now_ms, currency=qi.currency,
                min_margin_pct=qi.min_margin_pct, approval_over_cents=qi.approval_over_cents,
                customer_discount_pct=qi.customer_discount_pct)
            payload = qf.draft_quote_plan()
            payload["substitutes"] = list(qf.substitutes)
            payload["constraints"] = list(qf.constraints)
            confidence = 1.0                # deterministic over the resolved facts
        elif cap is Capability.REVENUE_LEAKAGE:
            leaks = self.source.leakages(subject)
            if leaks is None:
                return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "unknown subject")
            leaks = sorted(leaks, key=lambda l: (l.expected_value * l.confidence), reverse=True)
            payload = {
                "subject": subject, "count": len(leaks),
                "total_recoverable_cents": sum(int(l.amount_cents or 0) for l in leaks),
                "leakages": [_leak_dict(l) for l in leaks],
            }
            confidence = max((l.confidence for l in leaks), default=1.0)  # a clean scan is a confident answer
        else:
            return AcquisitionResult.failed(AcquisitionFailure.OUTSIDE_PROVIDER_COVERAGE,
                                            f"{cap.value} not served")
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=cap, subject=subject,
            observations=(payload,), retrieved_at=_now_iso(), freshness_s=0.0, confidence=confidence,
            cost=0.0, license_scope="internal", raw_response_digest=content_hash(payload))
        return AcquisitionResult.found((art,), cost=0.0)


def revenue_registry(source: RevenueStateSource) -> IntelligenceRegistry:
    """A registry with just the internal revenue-intelligence provider — the base every revenue DecisionNeed
    resolves against (paid enrichment, if any, registers on top in the private gateway)."""
    reg = IntelligenceRegistry()
    reg.register(RevenueIntelligenceProvider(source))
    return reg


# ── family synthesizer (turns the artifact into a business answer) ────────────────────────────────────────
def _usd(cents: int) -> str:
    return f"${cents / 100:,.0f}"


def revenue_synthesize(need: DecisionNeed, artifacts: tuple[EvidenceArtifact, ...]):
    """The Revenue family's answer synthesis (plugged into resolve_decision_need). States the feasibility /
    leakage plainly and surfaces approval requirements as gaps, so the broker never invents prose."""
    if not artifacts:
        return "", 0.0, {}, (), ("no evidence acquired",)
    a = artifacts[0]
    m = dict(a.observations[0])
    cap, confidence = need.capability, a.confidence
    gaps: list[str] = []
    if cap is Capability.QUOTE_FEASIBILITY:
        if m.get("feasible"):
            answer = (f"Quote feasible: {_usd(m['total_cents'])} {m['currency']} at "
                      f"{m['blended_margin_pct']:.0%} blended margin"
                      + (f", promised {m['promised_date_ms']}." if m.get("promised_date_ms") else "."))
        else:
            blockers = m.get("blockers") or []
            answer = ("Quote NOT feasible: " + ("; ".join(blockers) if blockers else "no fulfillment path")
                      + ".")
            gaps.extend(blockers)
        approvals = m.get("approval_requirements") or []
        if approvals:
            gaps.append(f"{len(approvals)} approval requirement(s): " + "; ".join(approvals))
    elif cap is Capability.REVENUE_LEAKAGE:
        n = m.get("count", 0)
        if n == 0:
            answer = "No recoverable revenue leakage detected for the subject."
        else:
            top = m["leakages"][0]
            answer = (f"{n} recoverable leakage signal(s), ~{_usd(m['total_recoverable_cents'])} at stake; "
                      f"top: {top['leakage_type']} on {top['subject']} — {top['proposed_action']}.")
    else:
        answer = ""
    if not need.meets_confidence(confidence):
        gaps.append("confidence below min_confidence")
    return answer, confidence, m, (), tuple(gaps)
