"""Small, dependency-free statistics: seeded bootstrap, rank correlations, rank flips. Pure functions."""
from __future__ import annotations

import math
import random
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


def mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs) if xs else float("nan")


def sd(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def percentile(sorted_xs: Sequence[float], q: float) -> float:
    if not sorted_xs:
        return float("nan")
    k = (len(sorted_xs) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return sorted_xs[lo] if lo == hi else sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo)


def ranks(xs: Sequence[float]) -> List[float]:
    """Average ranks (1 = smallest); ties share the mean rank."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def pearson(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or len(a) < 2:
        return float("nan")
    ma, mb = mean(a), mean(b)
    da = math.sqrt(math.fsum((x - ma) ** 2 for x in a))
    db = math.sqrt(math.fsum((x - mb) ** 2 for x in b))
    if da == 0 or db == 0:
        return float("nan")
    return math.fsum((x - ma) * (y - mb) for x, y in zip(a, b)) / (da * db)


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    return pearson(ranks(a), ranks(b))


def kendall_tau(a: Sequence[float], b: Sequence[float]) -> float:
    """Kendall tau-b."""
    n = len(a)
    if n != len(b) or n < 2:
        return float("nan")
    conc = disc = ta = tb = 0
    for i in range(n):
        for j in range(i + 1, n):
            da, db = a[i] - a[j], b[i] - b[j]
            if da == 0 and db == 0:
                continue
            if da == 0:
                ta += 1
            elif db == 0:
                tb += 1
            elif (da > 0) == (db > 0):
                conc += 1
            else:
                disc += 1
    den = math.sqrt((conc + disc + ta) * (conc + disc + tb))
    return (conc - disc) / den if den else float("nan")


def pairwise_flips(a: Mapping[str, float], b: Mapping[str, float]) -> Tuple[int, int, List[Tuple[str, str]]]:
    """Pairs of keys whose ORDER differs between two score maps: ``(flips, comparable_pairs, [(x, y)...])``."""
    keys = sorted(set(a) & set(b))
    flips: List[Tuple[str, str]] = []
    pairs = 0
    for i, x in enumerate(keys):
        for y in keys[i + 1:]:
            da, db = a[x] - a[y], b[x] - b[y]
            if da == 0 or db == 0:
                continue
            pairs += 1
            if (da > 0) != (db > 0):
                flips.append((x, y))
    return len(flips), pairs, flips


def bootstrap_means(groups: Mapping[str, Sequence[float]], *, n_boot: int = 1000, seed: int = 0) -> Dict[str, object]:
    """Paired bootstrap over INDEX positions: ``groups[name][i]`` must refer to the same unit (persona) i across
    groups (missing units must be dropped by the caller). Returns per-group mean/CI, the probability each group
    has the highest resampled mean (a stability measure of THIS synthetic panel, not demand), and each group's
    paired difference from the first group with a 95% percentile interval."""
    names = list(groups)
    n = len(next(iter(groups.values()))) if names else 0
    if n == 0 or any(len(v) != n for v in groups.values()):
        raise ValueError("bootstrap needs equal-length, non-empty paired groups")
    rng = random.Random(seed)
    boots: Dict[str, List[float]] = {k: [] for k in names}
    top = {k: 0.0 for k in names}
    diffs: Dict[str, List[float]] = {k: [] for k in names}
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        ms = {k: math.fsum(groups[k][i] for i in idx) / n for k in names}
        for k in names:
            boots[k].append(ms[k])
            diffs[k].append(ms[k] - ms[names[0]])
        best = max(ms.values())
        winners = [k for k in names if ms[k] == best]
        for k in winners:
            top[k] += 1.0 / len(winners)
    out: Dict[str, object] = {"n": n, "groups": {}}
    for k in names:
        s = sorted(boots[k])
        d = sorted(diffs[k])
        out["groups"][k] = {"mean": mean(groups[k]), "sd": sd(groups[k]), "ci95": [percentile(s, 0.025), percentile(s, 0.975)],
                            "p_top": top[k] / n_boot, "diff_vs_first": mean(groups[k]) - mean(groups[names[0]]),
                            "diff_ci95": [percentile(d, 0.025), percentile(d, 0.975)]}
    return out


def entropy(pmf: Sequence[float]) -> float:
    return -math.fsum(p * math.log(p, 2) for p in pmf if p > 0)


def effective_n(weights: Sequence[float]) -> float:
    s = math.fsum(weights)
    s2 = math.fsum(w * w for w in weights)
    return s * s / s2 if s2 else 0.0


__all__ = ["mean", "sd", "percentile", "ranks", "pearson", "spearman", "kendall_tau", "pairwise_flips",
           "bootstrap_means", "entropy", "effective_n"]
