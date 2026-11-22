"""ONE shared test for "is this reference a pointer to a synthetic study?" (SR-04).

Used by the first-party ledger guard, ``freeze_experiment``, ``plan_experiments``' explain counting and
``evidence_ref.attach/split`` so they cannot disagree about spelling tricks. A reference is normalised before it is
tested: NFKC (fullwidth forms), every format / control / private-use / unassigned character removed (zero-width and
bidi characters, soft hyphen, BOM, NUL), case-folded, surrounding blanks stripped, and a small table of Cyrillic /
Greek / Armenian look-alike letters folded to ASCII. The look-alike table is deliberately partial: it is a speed bump,
not a full confusables skeleton.

Two predicates, deliberately different:

* :func:`is_synthetic_ref` — the normalised text STARTS WITH ``synthetic-study:`` or ``synthetic_study:``. Used by the
  experiment-plane counters and seals. A reference that merely CONTAINS the scheme later in the text
  (``obs:synthetic-study:x``) is not one, exactly as before this change (it stays counted).
* :func:`mentions_synthetic_ref` — the normalised text CONTAINS either scheme ANYWHERE. Used only by the first-party
  ledger, which is allowed to be stricter than the experiment counters: nothing that mentions a synthetic study may
  sit in a first-party metric.
"""
from __future__ import annotations

import unicodedata

__all__ = ["normalize_ref", "is_synthetic_ref", "mentions_synthetic_ref", "SCHEMES"]

SCHEMES = ("synthetic-study:", "synthetic_study:")
_MAX_CHARS = 100_000
_DROP_CATEGORIES = ("Cf", "Cc", "Cs", "Co", "Cn")
_LOOKALIKES = str.maketrans({
    "ѕ": "s", "у": "y", "ү": "y", "һ": "h", "е": "e", "і": "i", "ı": "i", "с": "c", "ϲ": "c", "υ": "u", "ԁ": "d",
    "а": "a", "о": "o", "ο": "o", "р": "p", "т": "t", "п": "n", "ո": "n", "ν": "v",
})


def normalize_ref(ref: object) -> str:
    """The comparison form of a reference ('' for a non-string)."""
    if not isinstance(ref, str):
        return ""
    text = unicodedata.normalize("NFKC", ref[:_MAX_CHARS])
    text = "".join(ch for ch in text if unicodedata.category(ch) not in _DROP_CATEGORIES)
    return text.strip().casefold().translate(_LOOKALIKES)


def is_synthetic_ref(ref: object) -> bool:
    return normalize_ref(ref).startswith(SCHEMES)


def mentions_synthetic_ref(ref: object) -> bool:
    text = normalize_ref(ref)
    return any(s in text for s in SCHEMES)
