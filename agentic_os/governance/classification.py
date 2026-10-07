"""Data classification that SURVIVES transformation (Private Data Plane plan §3).

A restricted summary, embedding, extracted entity, trace or derived artifact stays restricted unless an explicit
declassification rule says otherwise. The levels are totally ordered; combining inputs takes the HIGHEST (a derived
artifact inherits the maximum classification of its sources), which is the core inheritance rule the whole plane
relies on. Pure.
"""
from __future__ import annotations

from enum import Enum
from typing import Iterable


class DataClassification(str, Enum):
    PUBLIC = "public"
    ENGINEERING = "engineering"              # source/IaC/public-docs/synthetic — may go to external coding models
    INTERNAL = "internal"
    CUSTOMER_CONFIDENTIAL = "customer_confidential"
    CUSTOMER_RESTRICTED = "customer_restricted"
    SECRET = "secret"

    @property
    def rank(self) -> int:
        return _ORDER.index(self)

    def __ge__(self, other):  # type: ignore[override]
        return self.rank >= other.rank if isinstance(other, DataClassification) else NotImplemented

    def __gt__(self, other):  # type: ignore[override]
        return self.rank > other.rank if isinstance(other, DataClassification) else NotImplemented

    def __le__(self, other):  # type: ignore[override]
        return self.rank <= other.rank if isinstance(other, DataClassification) else NotImplemented

    def __lt__(self, other):  # type: ignore[override]
        return self.rank < other.rank if isinstance(other, DataClassification) else NotImplemented


_ORDER = [
    DataClassification.PUBLIC, DataClassification.ENGINEERING, DataClassification.INTERNAL,
    DataClassification.CUSTOMER_CONFIDENTIAL, DataClassification.CUSTOMER_RESTRICTED, DataClassification.SECRET,
]

# The highest classification that an EXTERNAL (frontier) model/tool may ever receive. Anything above this is
# private business data and must stay in the trust boundary.
EXTERNAL_MAX = DataClassification.ENGINEERING


def max_classification(classes: Iterable[DataClassification]) -> DataClassification:
    """The inherited classification of a combined/derived artifact = the highest of its inputs (PUBLIC if none)."""
    hi = DataClassification.PUBLIC
    for c in classes:
        if c.rank > hi.rank:
            hi = c
    return hi


def externally_shareable(classification: DataClassification) -> bool:
    """True iff this classification may leave the trust boundary to an external processor."""
    return classification.rank <= EXTERNAL_MAX.rank


__all__ = ["DataClassification", "EXTERNAL_MAX", "max_classification", "externally_shareable"]
