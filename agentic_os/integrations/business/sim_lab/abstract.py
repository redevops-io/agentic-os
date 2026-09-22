"""The shared abstract decision space — the *only* vehicle by which a lesson learned in one business is
allowed to reach another.

The reframe demands the transferable lesson be **semantic**, not literal: "intervention value depends on
evidence of readiness, elapsed time, prior touches, and the cost of an unnecessary intervention" — never
"after N days send action X". So a lesson is expressed over these four domain-agnostic features and emits a
domain-agnostic *posture*; each world then translates a posture into its own native action. If a literal
Receivables threshold transferred to Stale-Quote we would suspect the simulator (the user's own test), so
literal transfer is kept only as a negative control (see ``transfer``).

Crucially the abstract space does NOT presume the two domains respond the *same way* to the same features.
Receivables and Stale-Quote can — and do — have opposite optimal responses to identical abstract features
(a debt with positive readiness pays itself, so HOLD; a quote with positive readiness must be actively
closed, so PUSH). Whether a lesson transfers is therefore an empirical question this space is designed to
answer honestly, including with a negative answer.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class Readiness(str, enum.Enum):
    POSITIVE = "positive"   # explicit forward engagement (promise to pay / asked to proceed)
    NEUTRAL = "neutral"     # silence / ambiguous
    NEGATIVE = "negative"   # explicit disengagement (won't pay / not interested)


class Elapsed(str, enum.Enum):
    EARLY = "early"
    MID = "mid"
    LATE = "late"


class Fatigue(str, enum.Enum):
    FRESH = "fresh"         # few or no prior touches
    WARM = "warm"
    SATURATED = "saturated" # already contacted many times — another touch risks harm


class Stakes(str, enum.Enum):
    LOW = "low"
    HIGH = "high"


class Posture(str, enum.Enum):
    """A domain-agnostic intervention stance. Each world maps these onto its own native actions; a world may
    have native actions no posture covers (that is where native learning beats transfer) and postures it has
    no native action for (that is where transfer silently under-serves)."""
    HOLD = "hold"                 # wait; do not contact
    GENTLE = "gentle"             # a light touch
    DECISIVE = "decisive"         # a firm, direct push
    CONCESSION = "concession"     # offer something / lower the ask
    ELICIT = "elicit"             # ask for information (Receivables has no such action — transfer can't emit it)
    HUMAN = "human"               # route to a person
    DISENGAGE = "disengage"       # stop investing (Receivables has no such action — transfer can't emit it)
    CONTESTED = "contested"       # a blocked/disputed case that must go to a human


@dataclass(frozen=True)
class AbstractFeatures:
    readiness: Readiness
    elapsed: Elapsed
    fatigue: Fatigue
    stakes: Stakes
    contested: bool = False

    def bucket(self) -> str:
        return f"{self.readiness.value[:3]}:{self.elapsed.value[:3]}:{self.fatigue.value[:3]}:" \
               f"{self.stakes.value[:1]}:{'c' if self.contested else '-'}"


def elapsed_of(days: int, *, mid: int = 30, late: int = 60) -> Elapsed:
    return Elapsed.EARLY if days < mid else Elapsed.MID if days < late else Elapsed.LATE


def fatigue_of(prior_touches: int) -> Fatigue:
    return Fatigue.FRESH if prior_touches <= 0 else Fatigue.WARM if prior_touches <= 2 else Fatigue.SATURATED


# A frozen PostureModel is just a mapping abstract-bucket -> posture, plus a default for unseen buckets.
@dataclass(frozen=True)
class PostureModel:
    policy: dict            # bucket -> Posture
    default: Posture = Posture.GENTLE
    source: str = ""        # provenance: which domain taught this

    def posture_for(self, feats: AbstractFeatures) -> Posture:
        if feats.contested:
            return Posture.CONTESTED
        return self.policy.get(feats.bucket(), self.default)
