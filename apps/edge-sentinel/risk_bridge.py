"""RiskTranslation bridge — the typed handoff to Agentic Compliance (Phase 8).

The boundary the plan draws: Edge Sentinel produces **evidence-backed technical facts** — scenario,
affected business service, likelihood/impact *basis* (descriptions grounded in evidence), assumptions and
uncertainty. **Agentic Compliance owns interpretation, calculation and reporting** (FAIR, risk matrices).
There is no FAIR engine here and Edge Sentinel never invents a financial input (the :class:`RiskTranslation`
contract itself refuses one).

There is not yet a Compliance-side *receiver* contract in the platform, so this phase produces the typed
handoff artifact and stops at the boundary; it does not invent a Compliance receiver. When Agentic
Compliance implements the receiver, it consumes this :class:`RiskHandoff` — and the ``RiskTranslation``
contract should graduate from ``apps/edge-sentinel`` to a shared home so both apps depend on one definition
rather than a copy. That is a deliberate follow-up, flagged rather than papered over.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .evidence import RiskTranslation, _now, sha256_hex


class RiskHandoffError(Exception):
    pass


@dataclass(frozen=True)
class RiskHandoff:
    """The envelope Edge Sentinel emits to Agentic Compliance. Carries the RiskTranslation + the source
    case, and makes 'assumptions were / were not stated' explicit rather than silent."""
    risk_translation: RiskTranslation
    source_case_id: str
    assumptions_stated: bool
    target_app: str = "agentic-compliance"
    handed_off_at: str = field(default_factory=_now)

    @property
    def handoff_id(self) -> str:
        return f"risk-{sha256_hex(self.source_case_id + '|' + '|'.join(self.risk_translation.technical_finding_refs))[:16]}"

    def to_dict(self) -> dict:
        return {"handoff_id": self.handoff_id, "target_app": self.target_app,
                "source_case_id": self.source_case_id, "assumptions_stated": self.assumptions_stated,
                "handed_off_at": self.handed_off_at, "risk_translation": self.risk_translation.to_dict()}


def build_risk_translation(store, case, *, affected_business_service: str, scenario: str,
                           likelihood_basis: str, impact_basis: str,
                           assumptions: tuple[str, ...] = (), uncertainty: str = "",
                           control_refs: tuple[str, ...] = ()) -> RiskTranslation:
    """Assemble a RiskTranslation from a case's evidence-backed findings. Pulls technical_finding_refs from
    the case and their evidence from the store, so the translation traces to technical evidence. Refuses to
    build from a case with no findings (nothing to translate), and never carries a financial input."""
    finding_refs = tuple(case.finding_refs)
    if not finding_refs:
        raise RiskHandoffError("cannot translate risk from a case with no findings")
    evidence_refs: list[str] = []
    for fid in finding_refs:
        f = store.findings.get(fid)
        if f:
            evidence_refs.extend(f.evidence_refs)
    if not evidence_refs:
        raise RiskHandoffError("risk translation must trace to technical evidence; none found")
    return RiskTranslation(
        technical_finding_refs=finding_refs, affected_business_service=affected_business_service,
        scenario=scenario, likelihood_basis=likelihood_basis, impact_basis=impact_basis,
        control_refs=control_refs, fair_inputs=None,  # never populated by Edge Sentinel
        uncertainty=uncertainty, assumptions=tuple(assumptions),
        evidence_refs=tuple(dict.fromkeys(evidence_refs)))


def emit_handoff(store, case, **kw) -> RiskHandoff:
    """Build the RiskTranslation and wrap it as a handoff to Agentic Compliance. ``assumptions_stated`` is
    explicit: missing assumptions do not silently pass — Compliance sees they were not provided."""
    rt = build_risk_translation(store, case, **kw)
    return RiskHandoff(risk_translation=rt, source_case_id=case.case_id,
                       assumptions_stated=bool(rt.assumptions))


def handoff_traces_to_evidence(store, handoff: RiskHandoff) -> bool:
    """Verify every evidence ref in the handoff resolves to real evidence in the store — an executive
    report built from this handoff traces back to technical evidence."""
    refs = handoff.risk_translation.evidence_refs
    return bool(refs) and all(r in store.evidence for r in refs)
