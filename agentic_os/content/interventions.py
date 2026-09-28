"""Content signals → governed intervention queue (Content & Search Intelligence plan §17, §19).

The search / behavior detectors surface *content opportunities*; the Priority Engine decides what to do about
each, under the same governance stance the revenue loop uses. This composes the two into the content approval
queue: every PROPOSE-status signal is lifted to a Priority-Engine `InterventionCandidate` and run through
`decide`, so each becomes an act / park-on-approval / abstain decision, ranked by transparent priority.

Publishing or rewriting a public page is an outbound, brand-affecting change, so every content candidate is
CONSEQUENTIAL and parks on approval — nothing here publishes anything. WATCH-status signals are *not* actions;
they're counted separately as things to keep observing (§7: observe before you act).

Duck-typed on the signal: it needs `signal_type` (an enum with `.value`), `affected_pages`, `confidence`,
`status`, `proposed_action`, and optionally `site_id` / `evidence` — satisfied by both `SearchSignal`
(GSC-derived) and `BehaviorSignal` (Umami-derived), so one queue spans both sources.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import (
    Action, InterventionCandidate, InterventionDecision, PriorityPolicy, decide,
)

# signal type → (capability the action needs, expected-value weight 0..1)
_ACTION = {
    "NEAR_WIN": ("content.page.strengthen", 0.70),
    "MISSING_PAGE": ("content.page.create", 0.60),
    "CTR_OPPORTUNITY": ("content.page.retitle", 0.55),
    "CANNIBALIZATION": ("content.page.consolidate", 0.50),
    "TOPIC_EXPANSION": ("content.topic.expand", 0.50),
    "EXISTING_INTENT": ("content.page.optimize", 0.40),
    "HIGH_TRAFFIC_LEVERAGE": ("content.page.leverage", 0.60),
    "UNDERPERFORMING_PAGE": ("content.page.improve", 0.45),
    # EMERGENT_INTENT is WATCH-only by construction and never becomes an action here.
}


def from_content_signal(signal: Any, *, source_app: str = "content") -> Optional[InterventionCandidate]:
    """Lift a PROPOSE-status content signal into a Priority-Engine candidate (CONSEQUENTIAL — a public-page
    change parks on approval). Returns None for a WATCH signal or an unmapped type — those aren't actions."""
    status = str(getattr(signal, "status", "") or "").upper()
    if status != "PROPOSE":
        return None
    kind = getattr(signal.signal_type, "value", str(signal.signal_type))
    mapped = _ACTION.get(kind)
    if mapped is None:
        return None
    capability, weight = mapped
    pages = tuple(getattr(signal, "affected_pages", ()) or ())
    subject = pages[0] if pages else str(getattr(signal, "query", "") or kind)
    return InterventionCandidate(
        source_app=source_app, subject=subject, proposed_action=signal.proposed_action,
        expected_value=weight, confidence=float(getattr(signal, "confidence", 0.0) or 0.0),
        urgency=0.5, execution_cost=0.2, attention_cost=0.2, risk_tier=RiskTier.CONSEQUENTIAL,
        reversibility=0.6, required_capabilities=(capability,),
        observation_refs=tuple(getattr(signal, "evidence", ()) or ()) + tuple(pages),
        candidate_id=f"content:{kind}:{subject}", action_kind=kind)


def plan_content_interventions(signals: Iterable[Any], *, policy: Optional[PriorityPolicy] = None,
                               source_app: str = "content") -> List[InterventionDecision]:
    """Turn content signals into ranked, governed intervention decisions (highest priority first). WATCH
    signals and abstentions are dropped — this is the actionable queue a human works top-down."""
    decisions: List[InterventionDecision] = []
    for sig in signals:
        cand = from_content_signal(sig, source_app=source_app)
        if cand is None:
            continue
        d = decide(cand, policy)
        if d.action is Action.ABSTAIN:
            continue
        decisions.append(d)
    decisions.sort(key=lambda d: d.priority.total, reverse=True)
    return decisions


def _decision_view(d: InterventionDecision) -> Dict[str, Any]:
    c = d.candidate
    return {
        "page": c.subject,
        "action": d.action.value,
        "requires_approval": d.requires_approval,
        "priority": round(d.priority.total, 4),
        "proposed_action": c.proposed_action,
        "required_capabilities": list(c.required_capabilities),
        "signal": c.action_kind,
        "rationale": d.rationale,
        "candidate_id": c.candidate_id,
        "observation_refs": list(c.observation_refs),
    }


def content_intervention_queue(signals: Iterable[Any], *, policy: Optional[PriorityPolicy] = None,
                               source_app: str = "content") -> Dict[str, Any]:
    """The full governed content queue as a serializable dict: ranked decisions + a summary, with WATCH
    signals counted separately (things to keep observing, not yet act on)."""
    sigs = list(signals)
    decisions = plan_content_interventions(sigs, policy=policy, source_app=source_app)
    views = [_decision_view(d) for d in decisions]
    approvals = sum(1 for d in decisions if d.requires_approval)
    watching = sum(1 for s in sigs if str(getattr(s, "status", "")).upper() == "WATCH")
    return {
        "count": len(views),
        "requires_approval": approvals,
        "auto": len(views) - approvals,
        "watching": watching,
        "decisions": views,
    }
