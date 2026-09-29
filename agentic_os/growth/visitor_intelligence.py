"""Visitor intelligence → lead-generation signals (Growth Intelligence — LEAD_GENERATION goal).

Turns visitor behavior (Umami page analytics) into lead opportunities: pages that express *commercial intent*
— pricing, demo, contact, get-started, quote, deploy — drawing real traffic are where visitors are closest to
becoming leads, so they're where lead capture / routing pays off. Each becomes a governed, approval-gated
`InterventionCandidate` (adding capture or routing visitors to sales is a real change, so it parks on approval).

Deterministic: the intent is read from the page path against a fixed lexicon; the strength is the page's own
traffic. A later slice can enrich with per-session identity (Umami sessions) to raise a known visitor straight
to an account re-engagement lead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import Action, InterventionCandidate, InterventionDecision, PriorityPolicy, decide

# page paths that signal commercial intent (a visitor close to becoming a lead)
_LEAD_INTENT = re.compile(
    r"/(pricing|plans|demo|contact|book|calendar|sign[-_]?up|register|get[-_]?started|start|trial|"
    r"quote|buy|checkout|deploy|apps-install|projects|book-a-call|talk-to)", re.I)
_CAP_CENTS = 0  # visitor signals carry no direct money; value is a normalized weight


@dataclass(frozen=True)
class VisitorSignal:
    subject: str                          # the page
    confidence: float
    status: str                           # PROPOSE | WATCH
    proposed_action: str
    action_kind: str = "LEAD_INTENT"
    evidence: Tuple[str, ...] = field(default_factory=tuple)
    site_id: str = ""


def lead_intent_pages(observations: Iterable[object], *, min_views: int = 10) -> List[VisitorSignal]:
    """High-intent pages (commercial-intent path + real traffic) → lead opportunities. Below `min_views` a
    page is WATCH (too little signal to act), above it PROPOSE."""
    out: List[VisitorSignal] = []
    for o in observations:
        url = getattr(o, "page_url", "") or ""
        views = int(getattr(o, "pageviews", 0) or 0)
        if views <= 0 or not _LEAD_INTENT.search(url):
            continue
        status = "PROPOSE" if views >= min_views else "WATCH"
        conf = round(min(0.9, 0.5 + 0.1 * (views / max(1, min_views))), 3)
        out.append(VisitorSignal(
            subject=url, confidence=conf, status=status,
            proposed_action="Capture / route the high-intent visitors on this page (add a CTA / lead form / "
                            "route to sales)",
            evidence=(f"umami:{views} views on a commercial-intent page",),
            site_id=getattr(o, "site_id", "") or ""))
    return out


def from_visitor_signal(signal: VisitorSignal, *, source_app: str = "growth") -> Optional[InterventionCandidate]:
    """Lift a PROPOSE-status lead-intent signal into a governed candidate. Enabling capture / routing visitors
    to sales is a real, outbound-facing change → CONSEQUENTIAL, parks on approval. WATCH → None."""
    if signal.status != "PROPOSE":
        return None
    return InterventionCandidate(
        source_app=source_app, subject=signal.subject, proposed_action=signal.proposed_action,
        expected_value=0.55, confidence=signal.confidence, urgency=0.5, execution_cost=0.2,
        attention_cost=0.2, risk_tier=RiskTier.CONSEQUENTIAL, reversibility=0.7,
        required_capabilities=("lead.capture.enable",), observation_refs=signal.evidence + (signal.subject,),
        candidate_id=f"lead:{signal.action_kind}:{signal.subject}", action_kind=signal.action_kind)


def plan_visitor_interventions(signals: Iterable[VisitorSignal], *, policy: Optional[PriorityPolicy] = None,
                               source_app: str = "growth") -> List[InterventionDecision]:
    """Turn lead-intent signals into ranked, governed decisions (approval-gated; abstentions dropped)."""
    decisions: List[InterventionDecision] = []
    for sig in signals:
        cand = from_visitor_signal(sig, source_app=source_app)
        if cand is None:
            continue
        d = decide(cand, policy)
        if d.action is not Action.ABSTAIN:
            decisions.append(d)
    decisions.sort(key=lambda d: d.priority.total, reverse=True)
    return decisions
