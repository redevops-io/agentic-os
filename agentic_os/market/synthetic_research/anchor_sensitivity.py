"""Sensitivity of SSR rankings to anchor selection and scoring parameters (pure; operates on embedded vectors).

Schemes computed for one (cell, construct) slice of response embeddings, always against the PRIMARY scheme (the
full local pack, paper-cosine, epsilon 0, T 1):

* ``leave_one_set_out`` — drop each anchor set in turn;
* ``single_set`` — each anchor set alone;
* ``shuffled_within_set`` — NEGATIVE CONTROL: anchors permuted inside each set (seeded), destroying the Likert
  ordering; a working scorer must collapse: the MEDIAN per-response correlation with the primary over many
  permutations should be near zero (``shuffle_null``); one permutation alone can keep much of the order;
* ``wrong_construct`` — NEGATIVE CONTROL: the same texts scored with another construct's pack;
* ``paraphrase_pack`` — a second, independently worded pack for the same construct (``anchors_alt``);
* ``eps_T`` sweeps — epsilon in {0, .01, .05, .1} x {paper-cosine, ref-halfcosine}, T in {0.5, 1, 2}.

Each scheme reports its variant means and ranking, the rank correlation with the primary ranking, the pairwise
order flips, whether the top variant changed and an ``unstable`` flag. These are diagnostics of THIS panel and
pack, not validity evidence.
"""
from __future__ import annotations

import random
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import stats
from .ssr import VARIANT_PAPER, VARIANT_REFERENCE, ssr_score

Vec = Sequence[float]
Sets = Sequence[Sequence[Vec]]


def _scores(rows: Sequence[Mapping[str, Any]], sets: Sets, variant: str, eps: float, temp: float) -> List[float]:
    return [ssr_score(r["vec"], sets, variant=variant, epsilon=eps, temperature=temp).mean for r in rows]


def _variant_means(rows: Sequence[Mapping[str, Any]], scores: Sequence[float]) -> Dict[str, float]:
    by: Dict[str, List[float]] = {}
    for r, s in zip(rows, scores):
        by.setdefault(r["variant_id"], []).append(s)
    return {k: stats.mean(v) for k, v in by.items()}


def _shuffle_sets(sets: Sets, seed: int) -> List[List[Vec]]:
    rng = random.Random(seed)
    out = []
    for s in sets:
        idx = list(range(5))
        for _ in range(20):
            rng.shuffle(idx)
            if idx != list(range(5)):
                break
        out.append([s[i] for i in idx])
    return out


def _compare(name: str, rows, scores, primary_scores, primary_means, *, note: str = "") -> Dict[str, Any]:
    means = _variant_means(rows, scores)
    keys = sorted(primary_means)
    a = [primary_means[k] for k in keys]
    b = [means[k] for k in keys]
    flips, pairs, flipped = stats.pairwise_flips(primary_means, means)
    top_p = max(primary_means, key=primary_means.get)
    top_s = max(means, key=means.get)
    rho = stats.spearman(a, b)
    return {"scheme": name, "note": note, "variant_means": means,
            "ranking": sorted(means, key=lambda k: -means[k]), "spread": max(means.values()) - min(means.values()),
            "spearman_vs_primary": rho, "kendall_vs_primary": stats.kendall_tau(a, b), "pairwise_flips": flips,
            "comparable_pairs": pairs, "reversed_pairs": [list(p) for p in flipped], "top_changed": top_p != top_s,
            "per_response_spearman_vs_primary": stats.spearman(scores, primary_scores),
            "unstable": bool(top_p != top_s or flips > 0)}


def anchor_sensitivity(rows: Sequence[Mapping[str, Any]], sets: Sets, *, alt_sets: Optional[Sets] = None,
                       wrong_sets: Optional[Sets] = None, seed: int = 0, n_shuffles: int = 20) -> Dict[str, Any]:
    """``rows``: dicts with ``variant_id`` and ``vec`` (the response embedding)."""
    if not rows:
        raise ValueError("no rows")
    prim = _scores(rows, sets, VARIANT_PAPER, 0.0, 1.0)
    pm = _variant_means(rows, prim)
    out: Dict[str, Any] = {"primary": {"variant_means": pm, "ranking": sorted(pm, key=lambda k: -pm[k]),
                                        "spread": max(pm.values()) - min(pm.values())}, "schemes": []}
    add = out["schemes"].append
    if len(sets) > 1:
        for k in range(len(sets)):
            rest = [s for j, s in enumerate(sets) if j != k]
            add(_compare(f"leave_out_set_{k}", rows, _scores(rows, rest, VARIANT_PAPER, 0.0, 1.0), prim, pm))
    for k in range(len(sets)):
        add(_compare(f"single_set_{k}", rows, _scores(rows, [sets[k]], VARIANT_PAPER, 0.0, 1.0), prim, pm))
    add(_compare("shuffled_within_set (NEGATIVE CONTROL)", rows, _scores(rows, _shuffle_sets(sets, seed), VARIANT_PAPER, 0.0, 1.0),
                 prim, pm, note="anchor order destroyed (one seeded permutation: a single permutation can preserve much of the order, "
                                "so see the permutation null below)"))
    nulls = sorted(stats.spearman(_scores(rows, _shuffle_sets(sets, seed * 1000 + k + 1), VARIANT_PAPER, 0.0, 1.0), prim)
                   for k in range(n_shuffles))
    out["shuffle_null"] = {"n_shuffles": n_shuffles, "per_response_spearman_median": nulls[len(nulls) // 2],
                           "per_response_spearman_min": nulls[0], "per_response_spearman_max": nulls[-1]}
    if wrong_sets is not None:
        add(_compare("wrong_construct_pack (NEGATIVE CONTROL)", rows, _scores(rows, wrong_sets, VARIANT_PAPER, 0.0, 1.0), prim, pm,
                     note="texts scored with another construct's anchors"))
    if alt_sets is not None:
        add(_compare("paraphrase_pack", rows, _scores(rows, alt_sets, VARIANT_PAPER, 0.0, 1.0), prim, pm,
                     note="second independently worded local pack, same construct"))
    for variant in (VARIANT_PAPER, VARIANT_REFERENCE):
        for eps in (0.0, 0.01, 0.05, 0.1):
            for temp in (0.5, 1.0, 2.0):
                if variant == VARIANT_PAPER and eps == 0.0 and temp == 1.0:
                    continue
                add(_compare(f"{variant} eps={eps} T={temp}", rows, _scores(rows, sets, variant, eps, temp), prim, pm))
    neg = [s for s in out["schemes"] if "NEGATIVE CONTROL" in s["scheme"] and "shuffled" not in s["scheme"]]
    out["negative_controls_collapse"] = (abs(out["shuffle_null"]["per_response_spearman_median"]) < 0.5
                                         and all(abs(s["per_response_spearman_vs_primary"]) < 0.5 for s in neg))
    out["n_unstable_schemes"] = sum(1 for s in out["schemes"] if s["unstable"] and "NEGATIVE" not in s["scheme"])
    out["n_schemes"] = len([s for s in out["schemes"] if "NEGATIVE" not in s["scheme"]])
    return out


__all__ = ["anchor_sensitivity"]
