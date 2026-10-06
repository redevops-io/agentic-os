"""WorkflowPain clustering + recurrence (Phase 3, plan §10).

Cluster by the underlying WORKFLOW, not vocabulary: "Stripe deposits never match QuickBooks", "how do you
reconcile Stripe fees when posting payouts to QBO", and "our bookkeeper spends hours matching Stripe payouts"
are one pain — payment-processor → accounting reconciliation. The clustering signature is the cross-app FRICTION
EDGE (the §20 application pair) plus the workflow action, so different wording over the same app pair collapses
together while genuinely different workflows stay apart.

Each cluster becomes a normalized ``WorkflowPain`` carrying its evidence ids, recurrence (first/last seen,
independent authors, source diversity via Phase-2 independence) and heuristic automation/agentic fit. Recurrence
and INDEPENDENT evidence — not raw mention volume — are the signal (plan §6/§11).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from .contracts import PainObservation, WorkflowPain
from .dedup import assess_independence

# workflow verbs that imply a DECISION/exception (→ higher agentic fit) vs a deterministic move
_DECISION_VERBS = frozenset({"compare", "price", "approve", "escalate", "decide", "monitor", "check", "match",
                             "reconcile", "renew", "follow up"})


def workflow_signature(pain: PainObservation) -> str:
    """Canonical key for the underlying workflow. ≥2 apps → the sorted app pair (the friction edge, §20); else
    the single app + its dominant action; else the action alone. Vocabulary differences inside the same edge
    collapse to one signature."""
    apps = tuple(sorted(set(pain.applications_mentioned)))
    verbs = tuple(sorted(set(pain.current_workflow)))
    if len(apps) >= 2:
        return "|".join(apps)
    if apps:
        return f"{apps[0]}:{verbs[0] if verbs else ''}"
    return verbs[0] if verbs else "unknown"


@dataclass(frozen=True)
class WorkflowCluster:
    workflow_pain: WorkflowPain
    signature: str
    raw_mentions: int
    independent_evidence_count: int
    independent_authors: int
    sources: Tuple[str, ...]
    communities: Tuple[str, ...]
    applications: Tuple[str, ...]
    first_seen: int
    last_seen: int

    @property
    def recurs(self) -> bool:
        return self.independent_evidence_count >= 2


def _pain_dimensions(members: Sequence[PainObservation]) -> Tuple[str, ...]:
    dims = set()
    for o in members:
        if o.frequency_hint:
            dims.add("repetitive")
        if o.manual_steps or o.failure_or_pain in ("manually", "by hand"):
            dims.add("manual")
        if o.cross_app:
            dims.add("cross_app")
        for c in o.consequence:
            dims.add(c)
    return tuple(sorted(dims))


def _fit(members: Sequence[PainObservation]) -> "Tuple[str, str]":
    cross_app = any(o.cross_app for o in members)
    verbs = {v for o in members for v in o.current_workflow}
    frequent = any(o.frequency_hint in ("daily", "per_order") for o in members)
    decisioned = bool(verbs & _DECISION_VERBS)
    automation_fit = "high" if (cross_app and frequent) else "medium" if cross_app else "low"
    # agentic (vs a deterministic one-line integration): needs cross-system context AND a decision/exception
    agentic_fit = "high" if (cross_app and decisioned) else "medium" if cross_app else "low"
    return automation_fit, agentic_fit


def cluster_pains(observations: Sequence[PainObservation], *, min_evidence: int = 1) -> List[WorkflowCluster]:
    """Group PainObservations into WorkflowPains by workflow signature; drop clusters below min independent
    evidence. Sorted by independent evidence, then recurrence span."""
    by_sig: Dict[str, List[PainObservation]] = {}
    for o in observations:
        by_sig.setdefault(workflow_signature(o), []).append(o)

    out: List[WorkflowCluster] = []
    for sig, members in by_sig.items():
        rep = assess_independence(members)
        if rep.independent_evidence_count < min_evidence:
            continue
        apps = tuple(sorted({a for o in members for a in o.applications_mentioned}))
        verbs = tuple(sorted({v for o in members for v in o.current_workflow}))
        consequences = tuple(sorted({c for o in members for c in o.consequence}))
        actor = next((o.actor for o in members if o.actor), "")
        automation_fit, agentic_fit = _fit(members)
        times = [o.published_at or o.prov.known_at or o.prov.observed_at for o in members]
        wp = WorkflowPain(
            prov=members[0].prov, name=sig, actor=actor,
            current_workflow=verbs, applications=apps, pain_dimensions=_pain_dimensions(members),
            business_consequences=consequences,
            workaround_classes=tuple(sorted({o.workaround for o in members if o.workaround})),
            automation_fit=automation_fit, agentic_fit=agentic_fit,
            evidence_ids=tuple(o.source_id or o.digest() for o in members))
        out.append(WorkflowCluster(
            workflow_pain=wp, signature=sig, raw_mentions=rep.raw_mentions,
            independent_evidence_count=rep.independent_evidence_count, independent_authors=rep.author_count,
            sources=tuple(sorted({o.source for o in members})),
            communities=tuple(sorted({o.community_or_topic for o in members if o.community_or_topic})),
            applications=apps, first_seen=min(times) if times else 0, last_seen=max(times) if times else 0))
    return sorted(out, key=lambda c: (c.independent_evidence_count, c.last_seen - c.first_seen), reverse=True)
