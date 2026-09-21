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
def external_action_view(request: AgentTaskRequest, *, decision_id: str = "",
                         receipt: Optional[Mapping[str, Any]] = None,
                         verification: str = "", task_state: str = "") -> dict:
    """A governed external action as Projects should show it — provenance + governance, no secrets."""
    r = dict(receipt or {})
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
        "approval": {"required": True, "decision_id": decision_id, "authorized": bool(decision_id)},
        "evidence_refs": list(request.bounded_context.get("finding_evidence", []) if isinstance(
            request.bounded_context.get("finding_evidence"), list) else
            ([request.bounded_context["finding_evidence"]] if request.bounded_context.get("finding_evidence") else [])),
        "task_state": task_state,
        "receipt": {"status": r.get("status", ""), "provider_post_id": r.get("external_id", ""),
                    "receipt_id": r.get("receipt_id", ""), "decision_id": r.get("decision_id", "")},
        "verification": verification,
        "failed": r.get("status") in ("FAILED", "HELD"),
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
