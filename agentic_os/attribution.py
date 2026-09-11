"""Outcome attribution — deriving an OutcomeEvent from an observation, only when it correlates with a
prior intervention, and with a MULTI-FACTOR attribution confidence (REAL_DATA_PROVIDER_SCOPING.md v2
§3.2/§3.4).

Delay is evidence, not the whole story. A reply two hours after one email is strongly attributable; a
reply two hours after (an email + an ad + a CEO who already knew the customer) is not; a contractual
renewal thirty days after a workflow may be strongly attributable. So attribution confidence is derived
from several factors, not from time delay alone:

    temporal_proximity      — closeness in time (delay IS one input, not the verdict)
    causal_link_strength    — plausibility of a causal path from action → outcome
    competing_interventions — other interventions that could explain it (MORE ⇒ LOWER confidence)
    explicit_correlation    — the outcome explicitly references the action (reply-to, execution_ref, …)
    outcome_specificity     — how specifically the outcome maps to this action vs. anything
    observation_quality     — how reliable the observing source is

The `OutcomeEvent` is DERIVED (not the connector's primary representation) once an observation is
correlated to an `InterventionRecord`; an `OutcomeLink` records the correlation immutably.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional, Sequence, Tuple

from agentic_os.intervention_record import InterventionRecord, OutcomeLink
from agentic_os.observation import Observation
from agentic_os.priority_engine import OutcomeEvent


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass(frozen=True)
class AttributionFactors:
    temporal_proximity: float = 0.0       # 0..1
    causal_link_strength: float = 0.0     # 0..1
    competing_interventions: float = 0.0  # 0..1 (1 = many equally-plausible alternatives)
    explicit_correlation: float = 0.0     # 0..1 (1 = the outcome names the action)
    outcome_specificity: float = 0.0      # 0..1
    observation_quality: float = 0.0      # 0..1


# a positive factor lifts confidence; competing_interventions DISCOUNTS it.
_DEFAULT_WEIGHTS = {
    "temporal_proximity": 0.20, "causal_link_strength": 0.25, "explicit_correlation": 0.25,
    "outcome_specificity": 0.15, "observation_quality": 0.15,
}


def attribution_confidence(f: AttributionFactors, weights: Optional[Mapping[str, float]] = None) -> float:
    """Combine the factors into a 0..1 confidence. Positive factors are a weighted mean; competing
    interventions multiplicatively discount it (if three other things could equally explain the
    outcome, you cannot confidently attribute it — no matter how quick or specific it was)."""
    w = weights or _DEFAULT_WEIGHTS
    pos = sum(w.get(k, 0.0) * getattr(f, k) for k in w)
    total_w = sum(w.values()) or 1.0
    base = pos / total_w
    return _clamp(base * (1.0 - 0.7 * _clamp(f.competing_interventions)))


def temporal_proximity(delay_seconds: float, half_life_seconds: float) -> float:
    """A soft proximity in [0,1]: 1.0 at zero delay, 0.5 at one half-life, decaying after. The
    half-life is per outcome type — a reply's is hours; a renewal's is weeks — so 'delay' is
    interpreted RELATIVE to what's expected, not as a raw penalty."""
    if delay_seconds < 0 or half_life_seconds <= 0:
        return 0.0
    return _clamp(0.5 ** (delay_seconds / half_life_seconds))


@dataclass(frozen=True)
class Attribution:
    intervention: InterventionRecord
    factors: AttributionFactors
    confidence: float


def correlate(observation: Observation, interventions: Sequence[InterventionRecord], *,
              half_life_seconds: float, causal_link_strength: float = 0.6,
              outcome_specificity: float = 0.6, observation_quality: float = 0.8,
              window_seconds: float = 30 * 24 * 3600.0) -> Optional[Attribution]:
    """Correlate an outcome observation to the best-matching prior intervention on the same subject,
    computing multi-factor attribution. Returns None when nothing plausibly matches (⇒ no OutcomeEvent
    is derived — the observation is still an EvidenceChange/StateDelta, just not an attributed outcome)."""
    # candidate interventions: same subject-ish (opportunity/candidate references the subject), executed
    # before the observation, within the window.
    cands: List[InterventionRecord] = []
    for r in interventions:
        acted_at = r.executed_at if r.executed_at is not None else r.proposed_at
        if acted_at is None or acted_at > observation.valid_at:
            continue
        if observation.valid_at - acted_at > window_seconds:
            continue
        if observation.subject and observation.subject not in (r.opportunity_id + " " + r.candidate_id):
            continue
        cands.append(r)
    if not cands:
        return None

    # nearest in time is the primary candidate; the rest are "competing"
    def acted(r):
        return r.executed_at if r.executed_at is not None else r.proposed_at
    cands.sort(key=lambda r: observation.valid_at - acted(r))
    primary = cands[0]
    competing = _clamp((len(cands) - 1) / 3.0)          # up to 3 competitors saturates the discount
    explicit = 1.0 if (primary.execution_ref and primary.execution_ref in
                       str(observation.payload.get("in_reply_to", "")) + str(observation.evidence_refs)) else 0.0
    factors = AttributionFactors(
        temporal_proximity=temporal_proximity(observation.valid_at - acted(primary), half_life_seconds),
        causal_link_strength=causal_link_strength, competing_interventions=competing,
        explicit_correlation=explicit, outcome_specificity=outcome_specificity,
        observation_quality=observation_quality)
    return Attribution(primary, factors, attribution_confidence(factors))


def derive_outcome(observation: Observation, attribution: Attribution, *,
                   reward_dimensions: Mapping[str, float]) -> Tuple[OutcomeEvent, OutcomeLink]:
    """Turn a correlated observation into an OutcomeEvent (for the learner) + an immutable OutcomeLink
    (for the intervention history). The reward dimensions come from the domain (the *_outcome mappers)."""
    r = attribution.intervention
    ev = OutcomeEvent(
        candidate_id=r.candidate_id, source_app=observation.source, action_kind=r.selected_action,
        reward_dimensions=dict(reward_dimensions),
        delay=max(0.0, observation.valid_at - (r.executed_at if r.executed_at is not None else r.proposed_at)),
        attribution_confidence=attribution.confidence,
        note=f"derived from observation {observation.observation_id} → intervention {r.intervention_id}")
    link = OutcomeLink(intervention_id=r.intervention_id, outcome_ref=observation.observation_id,
                       attribution_confidence=attribution.confidence, linked_at=observation.known_at)
    return ev, link
