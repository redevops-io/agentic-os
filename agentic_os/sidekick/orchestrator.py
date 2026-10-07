"""SidekickOrchestrator — the execution glue that makes capabilities actually run (plan §8/§24).

The registry (P1) made capabilities discoverable and the bindings (P5) made them callable, but nothing tied a
*request* to execution. This does: given an intent + inputs, it discovers the right capability, invokes its bound
handler, and returns a uniform SidekickResponse — or fans out across several capabilities and merges their results
deterministically (reusing merge_worker_results). Bindings stay pure (they return domain objects); the orchestrator
owns presentation via small projectors, so a handler's OfferDecision/FunnelDiagnosis/Deal becomes one response shape.

Pure + deterministic. An app wires this behind an HTTP endpoint and binds live-data handlers; anything consequential
still flows through Runtime authority/approval (candidate_actions are proposals).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from .capability import CapabilityMatch, CapabilityRegistry, default_registry
from .contracts import ArtifactLink, SidekickRequest, SidekickResponse
from .workers import Claim, MergedSidekickResult, SidekickWorkerResult, WorkerResultType, merge_worker_results


class CapabilityUnavailable(Exception):
    """Raised when a capability is known but has no bound handler (metadata-only descriptor)."""


@dataclass(frozen=True)
class CapabilityResult:
    """A uniform result envelope the orchestrator presents, regardless of the handler's domain return type."""
    capability_id: str
    answer: str = ""
    data: Any = None
    evidence: Tuple[str, ...] = ()
    candidate_actions: Tuple[Mapping[str, Any], ...] = ()
    artifacts: Tuple[ArtifactLink, ...] = ()
    confidence: float = 1.0


# ---- projectors: domain object → presentable fields (keeps bindings pure) --------------------------------------
def _project_offer(raw: Any) -> dict:
    sel = getattr(raw, "selected_offer", None)
    actions = ({"action": "select_offer", "offer_id": sel.offer_id, "score": getattr(raw, "expected_value", 0.0)},) \
        if sel else ({"action": "NO_ACTION"},)
    return {"answer": getattr(raw, "explanation", ""), "candidate_actions": actions,
            "confidence": 1.0 if sel else 0.0}


def _project_funnel(raw: Any) -> dict:
    stage = getattr(raw, "affected_stage", "")
    causes = getattr(raw, "candidate_causes", ())
    answer = f"Leakiest stage: {stage or 'none'}" + (f"; candidate causes: {', '.join(causes)}" if causes else "")
    return {"answer": answer, "evidence": tuple(getattr(raw, "evidence", ())),
            "candidate_actions": tuple({"action": "investigate_cause", "cause": c} for c in causes),
            "confidence": getattr(raw, "confidence", 0.0)}


def _project_campaign(raw: Any) -> dict:
    return {"answer": f"Campaign plan for '{getattr(raw, 'objective', '')}' → {getattr(raw, 'audience', '')}",
            "candidate_actions": ({"action": "request_campaign_approval"},) if getattr(raw, "requires_approval", False)
            else ({"action": "launch_campaign"},)}


def _project_deal_state(raw: Any) -> dict:
    conflicts = raw.conflicts() if hasattr(raw, "conflicts") else ()
    claims = getattr(raw, "claims", ())
    return {"answer": f"Verified {len(claims)} field(s); {len(conflicts)} conflicted.",
            "evidence": tuple(e for c in claims for e in getattr(c, "evidence_refs", ()))}


_PROJECTORS: Mapping[str, Callable[[Any], dict]] = {
    "acquisition.offer_decide": _project_offer,
    "acquisition.funnel_optimize": _project_funnel,
    "acquisition.campaign_plan": _project_campaign,
    "sales.deal_state": _project_deal_state,
}


@dataclass
class SidekickOrchestrator:
    """Discovers, invokes and merges capabilities for a Sidekick request."""
    registry: CapabilityRegistry = field(default_factory=lambda: default_registry)

    def discover(self, intent: str = "", *, available_inputs: Sequence[str] = (),
                 domain=None, limit: int = 5) -> Tuple[CapabilityMatch, ...]:
        return self.registry.discover(intent=intent, available_inputs=available_inputs, domain=domain, limit=limit)

    def invoke(self, capability_id: str, /, **inputs: Any) -> CapabilityResult:
        cap = self.registry.get(capability_id)
        if cap is None:
            raise KeyError(f"no such capability: {capability_id}")
        if not cap.bound:
            raise CapabilityUnavailable(f"{capability_id} has no bound handler")
        raw = cap.handler(**inputs)
        if isinstance(raw, CapabilityResult):
            return raw
        projected = _PROJECTORS.get(capability_id, lambda r: {})(raw)
        return CapabilityResult(
            capability_id=capability_id, data=raw,
            answer=projected.get("answer") or getattr(raw, "explanation", "") or f"Ran {capability_id}",
            evidence=tuple(projected.get("evidence", ())),
            candidate_actions=tuple(projected.get("candidate_actions", ())),
            confidence=float(projected.get("confidence", 1.0)))

    def handle(self, request: SidekickRequest, *, inputs: Optional[Mapping[str, Any]] = None,
               domain=None) -> SidekickResponse:
        """Route one request: discover the best BOUND capability for the message, invoke it, present a response.
        Returns a graceful 'no capability yet' response when nothing bound matches."""
        inputs = dict(inputs or {})
        matches = self.discover(request.user_message, available_inputs=tuple(inputs), domain=domain)
        chosen = next((m for m in matches if m.capability.bound), None)
        if chosen is None:
            discoverable = [m.capability.capability_id for m in matches[:3]]
            hint = f" (closest: {', '.join(discoverable)})" if discoverable else ""
            return SidekickResponse(answer=f"No runnable capability matches that yet{hint}.")
        res = self.invoke(chosen.capability.capability_id, **inputs)
        return SidekickResponse(answer=res.answer, evidence=res.evidence,
                                candidate_actions=res.candidate_actions, artifacts=res.artifacts)

    def run_parallel(self, specs: Sequence[Tuple[str, Mapping[str, Any]]], *, session_id: str = "",
                     mission_id: str = "") -> MergedSidekickResult:
        """Fan out across capabilities and merge deterministically (multi-Sidekick, §24). Each spec is
        (capability_id, inputs); results merge via the worker-merge engine (evidence union, actions ranked,
        contradictions preserved)."""
        results = []
        for cid, inp in specs:
            r = self.invoke(cid, **dict(inp))
            claims = (Claim(subject=cid, predicate="answer", value=r.answer, confidence=r.confidence,
                            worker_id=cid, evidence_refs=r.evidence),) if r.answer else ()
            results.append(SidekickWorkerResult(
                worker_id=cid, capability_id=cid, result_type=WorkerResultType.ANALYSIS,
                claims=claims, evidence_refs=r.evidence, candidate_actions=r.candidate_actions))
        return merge_worker_results(results, session_id=session_id, mission_id=mission_id)


__all__ = ["CapabilityResult", "CapabilityUnavailable", "SidekickOrchestrator"]
