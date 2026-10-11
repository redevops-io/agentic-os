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


def _project_order_execute(raw: Any) -> dict:
    """raw = Tuple[ReliabilityIncident]. Empty means every observed transition is healthy."""
    incidents = tuple(raw or ())
    if not incidents:
        return {"answer": "Order on track: no missed, overdue, or diverged transitions detected.",
                "candidate_actions": (), "confidence": 1.0}
    subjects = ", ".join(i.transition.subject for i in incidents)
    actions = tuple({"action": (i.transition.remediation_policy or "remediate"),
                     "subject": i.transition.subject, "failure": i.failure_type.value,
                     "status": i.status.value, "impact": i.impact} for i in incidents)
    return {"answer": f"{len(incidents)} order transition(s) at risk: {subjects}.",
            "evidence": tuple(f"{i.transition.subject}: observed={i.observed_state or 'none'}, "
                              f"{i.failure_type.value}/{i.status.value}" for i in incidents),
            "candidate_actions": actions,
            "confidence": 1.0}


def _project_service_plan(raw: Any) -> dict:
    """raw = ServicePlan."""
    svc = getattr(raw, "requested_service", "") or "service"
    feasible = bool(getattr(raw, "feasible", False))
    assigned = tuple(getattr(raw, "assigned", ()))
    risks = tuple(getattr(raw, "risks", ()))
    if feasible:
        answer = f"Service plan for '{svc}': feasible — {len(assigned)} assignment(s)."
        actions = ({"action": "schedule_service", "service": svc},)
    else:
        answer = f"Service plan for '{svc}': NOT feasible — {'; '.join(risks) or 'under-resourced'}."
        actions = ({"action": "resolve_resourcing", "service": svc},)
    return {"answer": answer,
            "evidence": tuple(f"{skill}->{rid}" for skill, rid in assigned),
            "candidate_actions": actions, "confidence": 1.0 if feasible else 0.0}


def _project_receivables(raw: Any) -> dict:
    """raw = ReceivableAssessment. `collect` is consequential (it dunns a customer), so it is proposed behind
    `request_approval` — never emitted as a directly-actionable step — matching the other consequential
    capabilities. Non-consequential next actions (resolve_dispute/resolve_delivery/wait/escalate/…) are proposed
    as-is. This keeps the governance explicit in the runnable path: when a blocker (e.g. an open dispute) is
    present, assess_receivable never returns `collect`, so no collection is ever proposed while disputed."""
    cause = getattr(raw, "probable_cause", "")
    action = getattr(raw, "next_action", None)
    action_val = getattr(action, "value", str(action) if action is not None else "none")
    rationale = getattr(raw, "rationale", "")
    materiality = getattr(raw, "materiality", 0.0)
    if action_val == "collect":
        actions: tuple = ({"action": "request_approval", "what": "collect", "materiality": materiality},)
    elif action_val in ("none", ""):
        actions = ()
    else:
        actions = ({"action": action_val, "materiality": materiality},)
    return {"answer": f"Probable cause: {cause}; next action: {action_val} — {rationale}.",
            "candidate_actions": actions, "confidence": 1.0}


def _project_forecast(raw: Any) -> dict:
    """raw = PipelineForecast. Propose verifying the deals whose committed value is capped below reported."""
    deals = tuple(getattr(raw, "deals", ()))
    flagged = tuple(d for d in deals if d.flags)
    actions = tuple({"action": "verify_deal", "opportunity_ref": d.opportunity_ref,
                     "at_risk_cents": d.reported_ev_cents - d.committed_ev_cents} for d in flagged)
    return {"answer": getattr(raw, "explanation", ""),
            "evidence": tuple(
                f"{d.opportunity_ref}: committed {d.committed_ev_cents}c of reported {d.reported_ev_cents}c"
                + (f" [{', '.join(d.flags)}]" if d.flags else "") for d in deals),
            "candidate_actions": actions, "confidence": 1.0}


def _project_segment(raw: Any) -> dict:
    """raw = SegmentationResult."""
    assignments = tuple(getattr(raw, "assignments", ()))
    return {"answer": getattr(raw, "explanation", ""),
            "evidence": tuple(f"{a.entity_id} ({a.display_name or 'n/a'}) -> {a.tier.value}"
                              + (f" [{', '.join(a.flags)}]" if a.flags else "") for a in assignments),
            "candidate_actions": tuple({"action": "assign_segment", "entity_id": a.entity_id, "tier": a.tier.value}
                                       for a in assignments),
            "confidence": 1.0}


def _project_market_observe(raw: Any) -> dict:
    """raw = MarketScan (observations + patterns + opportunities)."""
    opps = tuple(getattr(raw, "opportunities", ()))
    pats = tuple(getattr(raw, "patterns", ()))
    obs = getattr(raw, "observations", None)
    counts = obs.counts() if obs is not None and hasattr(obs, "counts") else {}
    answer = (f"Observed {counts.get('companies', 0)} company(ies); detected {len(pats)} pattern(s); "
              f"matched {len(opps)} opportunity(ies).")
    actions = tuple({"action": "flag_opportunity", "gap": o.gap, "site": o.site,
                     "expected_value": o.expected_value} for o in opps)
    evidence = tuple(f"pattern: {p.kind} — {p.description} (confidence {p.confidence})" for p in pats)
    confidence = max((o.confidence for o in opps), default=0.0) if opps else (1.0 if pats or counts else 0.0)
    return {"answer": answer, "evidence": evidence, "candidate_actions": actions, "confidence": confidence}


def _project_content_create(raw: Any) -> dict:
    """raw = ContentMissionRun.preview() dict (drafts produced, publish still gated)."""
    data = raw if isinstance(raw, dict) else {}
    drafts = data.get("drafts", [])
    channels = data.get("channels", [])
    concept = data.get("concept") or {}
    hook = concept.get("hook", "") if isinstance(concept, dict) else ""
    answer = (f"Drafted content for {len(drafts)} channel(s): {', '.join(channels)}."
              + (f" Hook: {hook}" if hook else ""))
    actions = ({"action": "request_publish_approval", "channels": channels},) if data.get("awaiting_approval") else ()
    evidence = tuple(f"{d.get('channel')}: {(d.get('text') or '')[:80]}" for d in drafts)
    return {"answer": answer, "evidence": evidence, "candidate_actions": actions,
            "confidence": 1.0 if drafts else 0.0}


_PROJECTORS: Mapping[str, Callable[[Any], dict]] = {
    "acquisition.offer_decide": _project_offer,
    "acquisition.funnel_optimize": _project_funnel,
    "acquisition.market_observe": _project_market_observe,
    "acquisition.content_create": _project_content_create,
    "acquisition.campaign_plan": _project_campaign,
    "sales.deal_state": _project_deal_state,
    "operations.order_execute": _project_order_execute,
    "operations.service_plan": _project_service_plan,
    "finance.receivables": _project_receivables,
    "commercial.forecast": _project_forecast,
    "commercial.segment": _project_segment,
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
