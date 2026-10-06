"""Workflow-pain extraction from a social observation (Phase 1).

The "broken cross-app workflow" classifier the Phase-0 audit identified as the net-new slot beside the existing
social problem/intent classifiers. Deterministic + keyword-driven over a versioned ``SearchUniverse`` (an LLM can
replace the markers later behind the same signature). It is DELIBERATELY conservative — returns None when there
is no first-person workflow-pain signal, so a feature wish or a promo post does not become evidence (plan §5, §27).

``evidence_strength`` is an honest proxy (first-person + specific apps + a stated frequency + an existing
workaround), never proof — the real signal is recurrence across independent observations (Phase 3) and, later,
our own validation.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

from ..agent_gateway.social.contracts import SocialObservation
from ..integrations.business.contracts import Provenance
from .contracts import PainObservation
from .vocabulary import (
    ACTOR_MARKERS, CONSEQUENCE_MARKERS, CROSS_APP_MARKERS, FREQUENCY_MARKERS, PAIN_MARKERS, SearchUniverse,
    WORKAROUND_MARKERS, WORKFLOW_VERBS, WTP_MARKERS, DEFAULT_UNIVERSE,
)

_FIRST_PERSON = ("i ", "i'", "we ", "we'", "our team", "our ", "my ")


def _author_hash(author_ref: str) -> str:
    return "ah_" + hashlib.sha256((author_ref or "").encode()).hexdigest()[:16] if author_ref else ""


def _frequency(text: str) -> str:
    for label, markers in FREQUENCY_MARKERS.items():
        if any(m in text for m in markers):
            return label
    return ""


def extract_pain(obs: SocialObservation, *, universe: SearchUniverse = DEFAULT_UNIVERSE,
                 extraction_version: str = "1") -> Optional[PainObservation]:
    """Extract a PainObservation from a social observation, or None if it is not a workflow pain."""
    text = (obs.text or "").lower()
    if not text:
        return None

    apps = tuple(a for a in universe.applications if a in text)
    # match verb STEMS so inflections count (reconcile → reconciling/reconciled), keeping the canonical verb
    verbs = tuple(v for v in universe.workflow_verbs if (v[:-1] if v.endswith("e") else v) in text)
    pains = tuple(m for m in universe.pain_markers if m in text)
    first_person = any(fp in text for fp in _FIRST_PERSON)

    # a workflow pain needs behavioural evidence: a pain/manual marker AND some workflow action or app.
    if not pains or not (verbs or apps):
        return None

    cross_app = len(apps) >= 2 or (bool(apps) and any(m in text for m in CROSS_APP_MARKERS))
    frequency = _frequency(text)
    workaround = next((w for w in WORKAROUND_MARKERS if w in text), "")
    wtp = any(m in text for m in WTP_MARKERS)
    actor = next((role for role, markers in ACTOR_MARKERS.items() if any(m in text for m in markers)), "")
    consequence = tuple(c for c, markers in CONSEQUENCE_MARKERS.items() if any(m in text for m in markers))

    strength = min(1.0, round(
        0.3 * first_person + 0.2 * min(1, len(apps)) + 0.15 * cross_app + 0.15 * bool(frequency)
        + 0.1 * bool(workaround) + 0.1 * bool(wtp) + 0.1 * min(1, len(pains) - 1 if len(pains) > 1 else 0), 4))

    prov = Provenance(provider=obs.provider, provider_ref=obs.source_ref,
                      evidence_refs=((obs.evidence_ref,) if obs.evidence_ref else ()),
                      observed_at=obs.observed_at, known_at=obs.published_at)
    return PainObservation(
        prov=prov, source=obs.provider, source_id=obs.source_ref, source_url=obs.provider_fields.get("url", ""),
        author_hash=_author_hash(obs.author_ref), community_or_topic=obs.provider_fields.get("community", ""),
        published_at=obs.published_at, actor=actor, consequence=consequence,
        applications_mentioned=apps, current_workflow=verbs,
        manual_steps=tuple(p for p in pains if p in ("manually", "by hand", "copy-paste", "copy paste")),
        failure_or_pain=pains[0] if pains else "", workaround=workaround, frequency_hint=frequency,
        willingness_to_pay_signal=wtp, cross_app=cross_app,
        text_tokens=tuple(sorted(set(re.sub(r"[^a-z0-9 ]", " ", text).split()))),
        evidence_strength=strength, extraction_confidence=round(0.4 + 0.5 * first_person + 0.1 * bool(apps), 4),
        extraction_version=extraction_version)
