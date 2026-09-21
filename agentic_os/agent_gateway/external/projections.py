"""Phase 9 — Projects/Sidekick projections (plan §11 Phase 9, §34-§35).

Client-safe views the Projects UI renders. The Phase 9 acceptance is that Projects stays the operational
source of truth and a Mission can be understood **without provider-private state** — so these projections
carry origin, provider, capability, approvals, evidence refs, receipt, verification and failures, and
NEVER secrets, cookies, provider tokens, or raw provider payloads.

The social projection makes the provider boundary visible (a source is AVAILABLE only when its
capability is VERIFIED/POLICY_SCOPED) and renders the complaint ≠ solution-seeking ≠ purchase-intent
distinction as explicit counts (plan §35), instead of leaving it a backend invariant.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from ..social.contracts import Signal, SocialOpportunity
from .contracts import AgentCapabilities, AgentTaskRequest


# ── external-agent action projection ─────────────────────────────────────────────────
def _evidence_list(bounded_context: Mapping[str, Any]) -> list:
    fe = bounded_context.get("finding_evidence")
    if isinstance(fe, list):
        return list(fe)
    return [fe] if fe else []


def external_action_view(request: AgentTaskRequest, *, approval_state: str = "pending",
                         decision_id: str = "", receipt: Optional[Mapping[str, Any]] = None,
                         verification: str = "", task_state: str = "") -> dict:
    """A governed external action as Projects should show it — provenance + governance, no secrets.

    ``approval_state`` is one of ``pending`` | ``authorized`` | ``denied`` and is rendered VERBATIM; the
    UI must not re-derive it. ``verification`` is passed through unchanged (``verified`` | ``abstained`` |
    ``refuted`` | ``n/a`` | ``""``) so UNKNOWN/abstention survives to the screen instead of collapsing to
    a boolean. ``failed`` is a convenience flag, but the UI should show ``receipt.status`` /
    ``verification`` literally rather than reconstructing pass/fail."""
    r = dict(receipt or {})
    if approval_state not in ("pending", "authorized", "denied"):
        raise ValueError(f"approval_state must be pending|authorized|denied, got {approval_state!r}")
    return {
        "origin": "external-agent",
        "provider": request.identity.provider,
        "adapter_version": request.identity.adapter_version,
        "principal": request.identity.principal.id if request.identity.principal else "",
        "project_id": request.project_id,
        "mission_id": request.mission_id,
        "capability": request.capability,
        "goal": request.goal,
        "intent_digest": request.intent_digest(),
        "approval": {"required": True, "state": approval_state, "decision_id": decision_id,
                     "authorized": approval_state == "authorized"},
        "evidence_refs": _evidence_list(request.bounded_context),
        "task_state": task_state,
        "receipt": {"status": r.get("status", ""), "provider_post_id": r.get("external_id", ""),
                    "receipt_id": r.get("receipt_id", ""), "decision_id": r.get("decision_id", "")},
        "verification": verification,
        "failed": r.get("status") in ("FAILED", "HELD"),
    }


def inspection_mission_view(inspection: Mapping[str, Any], governed_action: Optional[Mapping[str, Any]],
                            *, live: bool = True) -> dict:
    """The full "Inspect current ReDevOps demo deployments" card. Groups findings by severity for the UI
    (still rendering the projection's own severity strings — no client-side reconstruction) and states
    the boundary explicitly so 'receipt SUCCEEDED → verified' is never read as having changed the live
    SOC: live infrastructure is inspected read-only; the remediation is simulated through the governed
    fake adapter."""
    findings = list(inspection.get("findings", []))
    by_sev: dict = {}
    for f in findings:
        by_sev.setdefault(f.get("severity", "unknown"), []).append(f)
    return {
        "mission": "Deployment Inspection",
        "target": inspection.get("target", ""),
        "connected": inspection.get("connected", False),
        "boundary": {
            "inspection": "live" if live else "fixture",
            "remediation": "simulated (governed fake adapter — the live SOC is never mutated)",
        },
        "kpis": list(inspection.get("kpis", [])),
        "findings": findings,
        "findings_by_severity": {sev: by_sev.get(sev, []) for sev in ("critical", "high", "medium", "low")
                                 if sev in by_sev},
        "finding_count": len(findings),
        "proposed_actions": list(inspection.get("proposed_actions", [])),
        "governed_action": dict(governed_action) if governed_action else None,
    }


# ── social intelligence mission projection ───────────────────────────────────────────
def _source_availability(caps: AgentCapabilities, capability: str) -> str:
    st = caps.status_of(capability)
    if st.enabled:
        return "AVAILABLE" + (" / policy-scoped" if st.value == "POLICY_SCOPED" else "")
    return f"UNAVAILABLE — {st.value.lower().replace('_', ' ')}"


def social_mission_view(*, observations: int, opportunities: Sequence[SocialOpportunity],
                        market_signal_count: int,
                        provider_capabilities: Mapping[str, AgentCapabilities],
                        sources: Sequence[tuple]) -> dict:
    """The Social Intelligence Mission surface (plan §34-§35). ``sources`` is a sequence of
    ``(display_name, provider_key, capability_key)`` — each source is bound to its OWN provider, so a
    source whose provider is unverified reads UNAVAILABLE even if another provider supports the same
    capability."""
    source_views = []
    for display_name, provider_key, cap_key in sources:
        caps = provider_capabilities.get(provider_key)
        source_views.append({"source": display_name,
                             "status": _source_availability(caps, cap_key) if caps
                             else "UNAVAILABLE — provider not configured"})

    # complaint ≠ solution-seeking ≠ purchase-intent, as explicit counts
    problem = sum(1 for o in opportunities if o.problem.present is Signal.PRESENT)
    seeking = sum(1 for o in opportunities if o.intent.solution_seeking is Signal.PRESENT)
    commercial = sum(1 for o in opportunities if o.intent.commercial_intent is Signal.PRESENT)
    unknown_commercial = sum(1 for o in opportunities if o.intent.commercial_intent is Signal.UNKNOWN)

    return {
        "mission": "Social Intelligence",
        "sources": source_views,
        "observations": observations,
        "opportunities": {
            "total": len(opportunities),
            "problem_signals": problem,
            "solution_seeking": seeking,
            "commercial_intent_evidence": commercial,
            "unknown_commercial_intent": unknown_commercial,
        },
        "market_signals": market_signal_count,
        "proposed_actions": ["Draft response", "Create content mission", "Track topic"],
        # the cards, each safe to render (EXPLAIN-backed, no provider-private state)
        "opportunity_cards": [o.explain() for o in opportunities],
    }
