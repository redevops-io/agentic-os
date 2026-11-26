"""Measured reliability of an operation class, from independent verification, for the choice between "carry out", "gather more evidence" and
"ask a person".

This is a MEASUREMENT. It decides nothing and grants nothing: the permit / approval-required / deny decision belongs to the authorization
plane, which may take ``decision_input`` as one input. Two rules keep it honest:

* it counts only outcomes that an independent check confirmed or refuted (a read-back of the application's own record, a second process
  verifying a bundle), never the model's own stated confidence, which is not calibrated;
* with too few outcomes the answer is ``gather_evidence``, never ``reliable``: a perfect record of three is not a reliable operation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping

MIN_SAMPLES = 30
REQUIRED_LOWER_BOUND = 0.95
Z95 = 1.959964


@dataclass(frozen=True)
class Outcome:
    operation_class: str             # e.g. "erp.comment.add", "support.note.add"
    verified: bool                   # True: an independent read-back or verification confirmed it; False: it refuted it or could not confirm
    independent: bool = True         # False when the only evidence is the operation's own report; such outcomes are not counted


def wilson_lower(successes: int, n: int, z: float = Z95) -> float:
    """The lower end of the Wilson score interval: what the true rate is at least, with the usual 95 % confidence."""
    if n <= 0:
        return 0.0
    p = successes / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / d)


def measure(outcomes: Iterable[Outcome], *, min_samples: int = MIN_SAMPLES, required: float = REQUIRED_LOWER_BOUND) -> Dict[str, Dict[str, Any]]:
    tally: Dict[str, list] = {}
    ignored: Dict[str, int] = {}
    for o in outcomes:
        if not o.independent:
            ignored[o.operation_class] = ignored.get(o.operation_class, 0) + 1
            continue
        t = tally.setdefault(o.operation_class, [0, 0])
        t[0] += 1 if o.verified else 0
        t[1] += 1
    out: Dict[str, Dict[str, Any]] = {}
    for cls in sorted(set(tally) | set(ignored)):
        ok, n = tally.get(cls, [0, 0])
        low = wilson_lower(ok, n)
        out[cls] = {"verified": ok, "independent_outcomes": n, "not_independent": ignored.get(cls, 0), "rate": round(ok / n, 4) if n else None,
                    "lower_bound": round(low, 4), "min_samples": min_samples, "required_lower_bound": required,
                    "decision_input": "gather_evidence" if n < min_samples else ("reliable" if low >= required else "unreliable")}
    return out
