"""State estimation — observations are not facts.

The CRM, the inbox, a support ticket and a human will disagree. The runtime never writes an
observation straight into world state; it fuses possibly-conflicting observations of one key
into a confidence-weighted BELIEF, flagging conflict when high-confidence sources diverge. A
capability reads the belief; the planner can insert a disambiguation node when confidence is
low or `conflict` is set. This is the robotics sensor-fusion pattern for enterprise data.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .types import Belief, DecisionEvidence, now


def fuse(observations: list[dict], key: str = "") -> Belief:
    """Fuse observations [{value, source, confidence, source_type?, source_ref?}] of ONE key
    into a single Belief.

    Strategy: group by value, sum source-confidence per candidate, pick the strongest.
    Confidence = winner_mass / total_mass. `conflict` when a competing value also carries
    substantial (>=0.34 of total) mass — i.e. the sources genuinely disagree. Selection is by
    confidence mass only: NO ``source_type`` is privileged (never 'regex wins' / 'model wins').
    Each observation is also captured as a ``DecisionEvidence`` on the belief.
    """
    if not observations:
        return Belief(value=None, confidence=0.0, sources=[])

    evidence = [
        DecisionEvidence(
            field=key or o.get("key", ""),
            value=o.get("value"),
            source_type=o.get("source_type", "prior"),
            source_ref=o.get("source_ref", ""),
            confidence=float(o.get("confidence", 1.0)),
        )
        for o in observations
    ]

    mass: dict[Any, float] = defaultdict(float)
    maxconf: dict[Any, float] = defaultdict(float)   # reliability: the best single source per value
    src: dict[Any, list[str]] = defaultdict(list)
    latest: dict[Any, float] = defaultdict(float)
    value_of: dict[Any, Any] = {}
    for o in observations:
        v = o.get("value")
        key = _hashable(v)
        value_of[key] = v
        c = float(o.get("confidence", 1.0))
        mass[key] += c
        maxconf[key] = max(maxconf[key], c)
        s = o.get("source", "?")
        if s not in src[key]:
            src[key].append(s)
        latest[key] = max(latest[key], o.get("ts", 0.0) or 0.0)

    total = sum(mass.values()) or 1.0
    ranked = sorted(mass.items(), key=lambda kv: (kv[1], latest[kv[0]]), reverse=True)
    win_key, win_mass = ranked[0]
    runner_mass = ranked[1][1] if len(ranked) > 1 else 0.0
    # confidence = agreement across sources × reliability of the best source (capped at 1.0),
    # so BOTH a genuine disagreement and a single uncertain source read as low-confidence.
    agreement = win_mass / total
    reliability = min(1.0, maxconf[win_key])
    return Belief(
        value=value_of[win_key],
        confidence=round(agreement * reliability, 3),
        sources=src[win_key],
        conflict=runner_mass >= 0.34 * total,   # a competing value carries substantial mass
        updated_at=now(),
        evidence=evidence,
    )


def _hashable(v: Any):
    try:
        hash(v)
        return v
    except TypeError:
        return repr(v)
