"""Outcome derivation — turn an incoming observation into a persisted, attributed outcome
(PR-sequence.odt PR 4). NO learning here: this only correlates observations to prior interventions and
records the result.

Flow: an observation arrives (a reply, a booked meeting, an unsubscribe) → correlate it to a prior
:class:`~agentic_os.intervention_record.InterventionRecord` on the same subject
(:func:`agentic_os.attribution.correlate`, multi-factor) → if it correlates, derive an ``OutcomeEvent``
(with the causal-graph edges) and an immutable ``OutcomeLink``, and persist both: the OutcomeEvent to a
durable outcome store, the OutcomeLink into the intervention store. An observation that correlates to
nothing yields no outcome (it is still evidence, just not an attributed outcome).

The mapping from observation → reward dimensions is DOMAIN logic (is a reply positive or negative? is
this an unsubscribe?), so it is a pluggable ``reward_map`` seam; a default outreach mapper reads an
explicit ``payload['outcome']`` label. Learning consumes these persisted outcomes in a LATER PR.
"""
from __future__ import annotations

from typing import Callable, Dict, Mapping, Optional, Tuple

from agentic_os.attribution import correlate, derive_outcome
from agentic_os.observation import Observation
from agentic_os.priority_engine import OutcomeEvent

RewardMap = Callable[[Observation], Mapping[str, float]]

# outreach outcome taxonomy (odt §1): no response / positive / negative / unsubscribe / meeting / pilot
_OUTREACH_REWARDS: Dict[str, Dict[str, float]] = {
    "positive_reply": {"positive_reply": 1.0},
    "negative_reply": {"negative_reply": -1.0},
    "unsubscribe": {"unsubscribe": -1.0},
    "meeting": {"meeting": 1.0},
    "pilot_discussion": {"pilot_discussion": 1.0},
    "no_response": {},                       # a non-event carries no reward, but may still be recorded upstream
}


def outreach_reward_map(observation: Observation) -> Mapping[str, float]:
    """Default outreach mapper: use an explicit ``payload['outcome']`` label (set by the connector's
    classifier / a human), not a guess. An unlabelled observation yields no reward dimensions."""
    label = str(observation.payload.get("outcome", ""))
    return dict(_OUTREACH_REWARDS.get(label, {}))


HALF_LIVES = {          # per-outcome half-life for temporal proximity (odt §3/§4: delay is relative)
    "positive_reply": 24 * 3600.0, "negative_reply": 24 * 3600.0, "unsubscribe": 24 * 3600.0,
    "meeting": 7 * 24 * 3600.0, "pilot_discussion": 30 * 24 * 3600.0,
}
_DEFAULT_HALF_LIFE = 3 * 24 * 3600.0


def derive_and_persist(observation: Observation, *, intervention_store, outcome_store,
                       reward_map: RewardMap = outreach_reward_map,
                       causal_link_strength: float = 0.6) -> Optional[Tuple[OutcomeEvent, object]]:
    """Correlate an observation to prior interventions and, if attributed, persist the derived outcome.
    Returns (OutcomeEvent, OutcomeLink) or None when nothing correlated / there's no reward to record."""
    dims = reward_map(observation)
    if not dims:
        return None                          # not an outcome-bearing observation (e.g. unlabelled/no-response)
    half_life = HALF_LIVES.get(str(observation.payload.get("outcome", "")), _DEFAULT_HALF_LIFE)
    attribution = correlate(observation, intervention_store.all(), half_life_seconds=half_life,
                            causal_link_strength=causal_link_strength)
    if attribution is None:
        return None                          # correlated to no prior intervention ⇒ not an attributed outcome
    ev, link = derive_outcome(observation, attribution, reward_dimensions=dims)
    outcome_store.append(ev)                 # durable outcome (for the future learner)
    intervention_store.link(link)            # immutable correlation edge on the intervention history
    return ev, link


def outcome_derivation_sink(intervention_store, outcome_store, *, reward_map: RewardMap = outreach_reward_map):
    """An ObservationIngestor sink that derives+persists an outcome for every ingested observation that
    correlates to a prior intervention. Plug it into ``build_persisting_ingestor(..., extra_sinks=[…])``
    so one path persists the observation AND closes the outcome loop (still no learning)."""
    def _sink(obs, deltas, changes):
        derive_and_persist(obs, intervention_store=intervention_store, outcome_store=outcome_store,
                           reward_map=reward_map)
    return _sink
