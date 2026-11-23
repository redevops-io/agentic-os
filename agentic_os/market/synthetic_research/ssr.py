"""Semantic Similarity Rating — the scoring core (pure Python, deterministic, no I/O, no dependencies).

Reference: Maier et al., "LLMs Reproduce Human Purchase Intent via Semantic Similarity Elicitation of Likert
Ratings", arXiv:2510.08338v3, App. A.4.3 (CC BY 4.0). For a response embedding ``t`` and one reference set of
five anchor statements ``sigma_1..sigma_5`` (one per Likert point):

    gamma_r = similarity(sigma_r, t)                                   (eq. 7: cosine in the paper)
    p(r)  ∝  gamma_r − gamma_min + epsilon · [r == argmin]              (eq. 8, normalised to sum 1)
    p(r,T) ∝ p(r) ** (1/T)                                             (eq. 9, optional)

and the final pmf is the mean of the per-set pmfs. Two similarity variants are provided and ALWAYS reported:

* ``paper-cosine``   — plain cosine, as printed in the paper (eq. 7);
* ``ref-halfcosine`` — (1 + cosine) / 2, as in the authors' reference package (Apache-2.0).

With ``epsilon == 0`` the two give IDENTICAL pmfs (minimum subtraction and normalisation cancel the affine map);
they differ only when ``epsilon > 0``. Fidelity limits, stated once and repeated in every report: the paper does
not publish its six anchor sets, so any anchor pack used with this core is a LOCAL pack (see ``anchors.py``);
parity with the paper's reported numbers is therefore not testable here.

Nothing in this module reads a network, a file or an environment variable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

LIKERT_POINTS = 5
VARIANT_PAPER = "paper-cosine"
VARIANT_REFERENCE = "ref-halfcosine"
VARIANTS = (VARIANT_PAPER, VARIANT_REFERENCE)

Vector = Sequence[float]


class SSRError(ValueError):
    """Invalid input (shape, non-finite numbers, zero vectors, bad parameters). Never silently repaired."""


def _check_vector(v: Vector, what: str) -> None:
    if len(v) == 0:
        raise SSRError(f"{what}: empty vector")
    for x in v:
        if not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x):
            raise SSRError(f"{what}: non-finite or non-numeric component")


def cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity in [-1, 1]. Raises on a length mismatch, empty/non-finite input or a zero vector."""
    _check_vector(a, "a")
    _check_vector(b, "b")
    if len(a) != len(b):
        raise SSRError(f"dimension mismatch: {len(a)} != {len(b)}")
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        raise SSRError("zero-norm vector: cosine is undefined")
    c = math.fsum(x * y for x, y in zip(a, b)) / (na * nb)
    return max(-1.0, min(1.0, c))          # clamp rounding excursions only


def similarity(a: Vector, b: Vector, variant: str = VARIANT_PAPER) -> float:
    if variant not in VARIANTS:
        raise SSRError(f"unknown similarity variant {variant!r}")
    c = cosine(a, b)
    return c if variant == VARIANT_PAPER else (1.0 + c) / 2.0


def pmf_from_similarities(sims: Sequence[float], epsilon: float = 0.0) -> Tuple[List[float], bool]:
    """Equation 8 for ONE reference set. Returns ``(pmf, degenerate)``.

    ``degenerate`` is True when all five similarities are equal (after subtracting the minimum every numerator
    is 0, so the pmf is undefined for ``epsilon == 0``): the pmf is then the UNIFORM distribution and the caller
    is told so (a flat result is a diagnostic, not a measurement). With ``epsilon > 0`` the mass sits on the
    argmin position (first index on ties), exactly as the equation reads.
    """
    if len(sims) != LIKERT_POINTS:
        raise SSRError(f"expected {LIKERT_POINTS} similarities, got {len(sims)}")
    if epsilon < 0 or not math.isfinite(epsilon):
        raise SSRError("epsilon must be a finite number >= 0")
    for s in sims:
        if not math.isfinite(s):
            raise SSRError("non-finite similarity")
    lo = min(sims)
    num = [s - lo for s in sims]
    if epsilon > 0:
        num[sims.index(lo)] += epsilon
    total = math.fsum(num)
    if total <= 0.0:
        return [1.0 / LIKERT_POINTS] * LIKERT_POINTS, True
    return [x / total for x in num], False


def temperature_scale(pmf: Sequence[float], temperature: float) -> List[float]:
    """Equation 9: p ** (1/T), renormalised (log-space, so tiny probabilities and small T do not overflow).

    ``T == 1`` returns the input unchanged; ``T -> 0`` approaches the one-hot argmax (``T == 0`` returns exactly
    the one-hot, uniform on ties); ``T > 1`` smooths. Zero probabilities stay zero for every finite ``T``.
    """
    if len(pmf) != LIKERT_POINTS:
        raise SSRError("pmf must have five entries")
    if temperature < 0 or not math.isfinite(temperature):
        raise SSRError("temperature must be a finite number >= 0")
    if any(p < 0 or not math.isfinite(p) for p in pmf):
        raise SSRError("invalid pmf")
    if temperature == 1.0:
        return list(pmf)
    if temperature == 0.0:
        top = max(pmf)
        winners = [i for i, p in enumerate(pmf) if p == top]
        return [1.0 / len(winners) if i in winners else 0.0 for i in range(LIKERT_POINTS)]
    logs = [math.log(p) / temperature if p > 0 else -math.inf for p in pmf]
    m = max(logs)
    exps = [math.exp(x - m) if x != -math.inf else 0.0 for x in logs]
    s = math.fsum(exps)
    return [x / s for x in exps]


def mean_rating(pmf: Sequence[float]) -> float:
    """Expected Likert value (1..5) of a pmf."""
    if len(pmf) != LIKERT_POINTS:
        raise SSRError("pmf must have five entries")
    return math.fsum((i + 1) * p for i, p in enumerate(pmf))


def mean_pmf(pmfs: Sequence[Sequence[float]]) -> List[float]:
    """Arithmetic mean of pmfs (per-set averaging within a response, and survey aggregation over respondents)."""
    if not pmfs:
        raise SSRError("no pmfs to average")
    for p in pmfs:
        if len(p) != LIKERT_POINTS:
            raise SSRError("pmf must have five entries")
    return [math.fsum(p[i] for p in pmfs) / len(pmfs) for i in range(LIKERT_POINTS)]


def ks_similarity(p: Sequence[float], q: Sequence[float]) -> float:
    """Distributional similarity K_xy = 1 − KS distance between two Likert pmfs (the paper's success metric)."""
    if len(p) != LIKERT_POINTS or len(q) != LIKERT_POINTS:
        raise SSRError("pmf must have five entries")
    cp = cq = 0.0
    worst = 0.0
    for a, b in zip(p, q):
        cp += a
        cq += b
        worst = max(worst, abs(cp - cq))
    return 1.0 - worst


@dataclass(frozen=True)
class SSRResult:
    """One response scored against one anchor pack. All fields are plain, serialisable values."""
    pmf: Tuple[float, ...]                    # final pmf (mean of per-set pmfs, after temperature)
    mean: float                               # expected Likert value of ``pmf``
    per_set_pmfs: Tuple[Tuple[float, ...], ...]
    degenerate_sets: int                      # how many anchor sets produced a flat (undefined) pmf
    variant: str
    epsilon: float
    temperature: float

    @property
    def is_degenerate(self) -> bool:
        return self.degenerate_sets == len(self.per_set_pmfs)


def ssr_score(response_embedding: Vector, anchor_set_embeddings: Sequence[Sequence[Vector]], *,
              variant: str = VARIANT_PAPER, epsilon: float = 0.0, temperature: float = 1.0) -> SSRResult:
    """Score one response embedding against ``m`` anchor sets of five anchor embeddings each.

    Deterministic: identical inputs give a bit-identical result. The temperature is applied to each per-set pmf
    (equation 9 is stated per set) and the scaled pmfs are then averaged.
    """
    if not anchor_set_embeddings:
        raise SSRError("at least one anchor set is required")
    per_set: List[Tuple[float, ...]] = []
    degenerate = 0
    for k, anchors in enumerate(anchor_set_embeddings):
        if len(anchors) != LIKERT_POINTS:
            raise SSRError(f"anchor set {k}: expected {LIKERT_POINTS} anchors, got {len(anchors)}")
        sims = [similarity(a, response_embedding, variant) for a in anchors]
        p, flat = pmf_from_similarities(sims, epsilon)
        degenerate += 1 if flat else 0
        per_set.append(tuple(temperature_scale(p, temperature)))
    final = mean_pmf(per_set)
    return SSRResult(pmf=tuple(final), mean=mean_rating(final), per_set_pmfs=tuple(per_set),
                     degenerate_sets=degenerate, variant=variant, epsilon=epsilon, temperature=temperature)


__all__ = ["LIKERT_POINTS", "VARIANT_PAPER", "VARIANT_REFERENCE", "VARIANTS", "SSRError", "SSRResult", "cosine",
           "similarity", "pmf_from_similarities", "temperature_scale", "mean_rating", "mean_pmf",
           "ks_similarity", "ssr_score"]
