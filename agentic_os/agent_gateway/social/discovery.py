"""S3/S4/S5 — Discovery reasoning: observations → signals → opportunities → market signals.

Discovery owns evidence-to-opportunity reasoning (plan §32); the social adapter carries none of it. The
classification is deterministic and evidence-backed (matched phrases are the evidence), and it ABSTAINS
(UNKNOWN) rather than inventing a signal. Three separations are enforced:

  * a problem is not solution-seeking; solution-seeking is not commercial intent (plan §31);
  * ranking is explainable component scores, not one opaque lead score (plan §25);
  * every aggregate MarketSignal traces to source observations (plan §26).

Social text is UNTRUSTED (plan §37): phrases are only ever matched as data — never executed — so an
embedded "ignore previous instructions …" injection contributes nothing but its literal words.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .contracts import (
    IntentSignal, MarketSignal, ProblemSignal, Signal, SocialObservation, SocialOpportunity)

# Evidence phrase sets (transparent, auditable — the matched phrases ARE the cited evidence).
_PROBLEM_MARKERS = ("trying to", "frustrating", "garbage", "losing", "loses", "forgetting", "forget",
                    "keeps", "again", "struggl", "broken", "doesn't work", "can't get")
_SOLUTION_SEEKING = ("is there anything", "recommendation", "recommend", "any alternative", "alternative",
                     "what works", "actually works", "how do i", "suggestions", "looking for")
_COMMERCIAL_INTENT = ("budget approved", "evaluating vendor", "replace our", "switching", "switch from",
                      "pricing", "quote", "buy", "purchase", "procurement", "vendor")
_INJECTION_MARKERS = ("ignore all previous", "ignore previous instructions", "disregard previous")

_DAY = 86_400.0


def _matches(text: str, markers: Sequence[str]) -> List[str]:
    low = text.lower()
    return [m for m in markers if m in low]


def is_injection(obs: SocialObservation) -> bool:
    """Flag prompt-injection content so the UI/Learn can quarantine it. It is still classified only as
    data; this never changes control flow beyond a surfaced flag."""
    return bool(_matches(obs.text, _INJECTION_MARKERS))


def classify_problem(obs: SocialObservation) -> ProblemSignal:
    hits = _matches(obs.text, _PROBLEM_MARKERS)
    if hits:
        return ProblemSignal(Signal.PRESENT, description=f"problem markers: {', '.join(hits[:3])}",
                             evidence_refs=(obs.evidence_ref,))
    # A question with no problem markers is genuinely ambiguous → abstain, don't assert ABSENT.
    if "?" in obs.text:
        return ProblemSignal(Signal.UNKNOWN, description="question without explicit problem markers",
                             evidence_refs=(obs.evidence_ref,))
    return ProblemSignal(Signal.ABSENT, description="no problem markers")


def classify_intent(obs: SocialObservation) -> IntentSignal:
    seeking = _matches(obs.text, _SOLUTION_SEEKING) or ("?" in obs.text and _matches(obs.text, _PROBLEM_MARKERS))
    commercial = _matches(obs.text, _COMMERCIAL_INTENT)
    return IntentSignal(
        solution_seeking=Signal.PRESENT if seeking else Signal.UNKNOWN,   # absence of evidence ≠ ABSENT
        commercial_intent=Signal.PRESENT if commercial else Signal.UNKNOWN,
        evidence_refs=(obs.evidence_ref,))


def assess_product_fit(obs: SocialObservation, product_terms: Sequence[str]) -> Signal:
    hits = _matches(obs.text, [t.lower() for t in product_terms])
    if hits:
        return Signal.PRESENT
    return Signal.UNKNOWN


def _freshness_score(freshness_seconds: Optional[float]) -> float:
    if freshness_seconds is None:
        return 0.3                       # unknown recency → neutral-low, not zero
    # 1.0 at t=0, ~0.5 at one day, decaying — deterministic.
    return round(1.0 / (1.0 + freshness_seconds / _DAY), 4)


def to_opportunity(obs: SocialObservation, *, product_terms: Sequence[str],
                   now_ms: Optional[int] = None) -> Optional[SocialOpportunity]:
    """Build an evidence-backed opportunity, or return None when there is no problem evidence at all
    (abstain rather than manufacture a lead). Component scores are explainable and replayable."""
    problem = classify_problem(obs)
    if problem.present is Signal.ABSENT:
        return None
    intent = classify_intent(obs)
    fit = assess_product_fit(obs, product_terms)
    fresh = obs.freshness_seconds(now_ms=now_ms)

    components: Dict[str, float] = {
        "problem_relevance": 1.0 if problem.present is Signal.PRESENT else 0.4,
        "solution_seeking": 1.0 if intent.solution_seeking is Signal.PRESENT else 0.0,
        "commercial_intent": 1.0 if intent.commercial_intent is Signal.PRESENT else 0.0,
        "product_fit": 1.0 if fit is Signal.PRESENT else 0.3,
        "freshness": _freshness_score(fresh),
        "specificity": min(1.0, len(obs.text) / 200.0),
        "evidence_quality": 1.0 if obs.public_visibility and obs.provider_policy_ref else 0.5,
    }
    # Confidence is an explicit weighted blend; EXPLAIN exposes the parts so nothing hides in one score.
    weights = {"problem_relevance": 0.25, "solution_seeking": 0.2, "commercial_intent": 0.15,
               "product_fit": 0.2, "freshness": 0.1, "specificity": 0.05, "evidence_quality": 0.05}
    confidence = round(sum(components[k] * w for k, w in weights.items()), 4)

    evidence_suff = (Signal.PRESENT if (problem.present is Signal.PRESENT and fit is Signal.PRESENT)
                     else Signal.UNKNOWN)
    actions: Tuple[str, ...] = ("review_as_lead_candidate", "propose_useful_response") \
        if intent.solution_seeking is Signal.PRESENT else ("add_to_market_signal_cluster",)

    return SocialOpportunity.new(
        provider=obs.provider, source_ref=obs.source_ref, subject_ref=obs.author_ref,
        problem=problem, intent=intent, product_fit=fit, freshness_seconds=fresh,
        evidence_sufficiency=evidence_suff, component_scores=components, confidence=confidence,
        proposed_actions=actions, evidence_refs=(obs.evidence_ref,))


def rank_opportunities(opps: Sequence[SocialOpportunity]) -> List[SocialOpportunity]:
    """Deterministic, replayable ranking (confidence desc, then id for stable ties)."""
    return sorted(opps, key=lambda o: (-o.confidence, o.opportunity_id))


# ── S5: market intelligence ──────────────────────────────────────────────────────────
def _topic_key(text: str) -> str:
    words = re.findall(r"[a-z]{4,}", text.lower())
    stop = {"that", "this", "with", "your", "have", "keep", "keeps", "there", "anything", "actually",
            "again", "trying", "three", "days", "customer", "previous"}
    key = tuple(sorted({w for w in words if w not in stop})[:6])
    return "|".join(key)


def market_signals(observations: Sequence[SocialObservation], *, window_seconds: int,
                   now_ms: Optional[int] = None) -> List[MarketSignal]:
    """Cluster observations into recurring-topic signals. Duplicates (same topic key) fold into one
    cluster but the unique-thread count reflects distinct threads. Every signal keeps its evidence."""
    clusters: Dict[str, List[SocialObservation]] = {}
    for o in observations:
        clusters.setdefault(_topic_key(o.text), []).append(o)

    signals: List[MarketSignal] = []
    for key, obs_list in clusters.items():
        if not key:
            continue
        source_mix: Dict[str, int] = {}
        for o in obs_list:
            source_mix[o.provider] = source_mix.get(o.provider, 0) + 1
        threads = {o.thread_ref for o in obs_list}
        signals.append(MarketSignal(
            topic=key.replace("|", " "), window_seconds=window_seconds,
            observation_count=len(obs_list), unique_thread_count=len(threads), source_mix=source_mix,
            trend="rising" if len(obs_list) > 1 else "flat",
            representative_evidence_refs=tuple(o.evidence_ref for o in obs_list[:3]),
            confidence=round(min(1.0, len(threads) / 3.0), 4)))
    return sorted(signals, key=lambda s: (-s.observation_count, s.topic))
