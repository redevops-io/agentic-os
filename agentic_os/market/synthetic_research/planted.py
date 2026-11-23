"""PLANTED-ORDER benchmark: does a method recover a quality ordering that is known BY CONSTRUCTION?

For a base concept we build four variants whose ordering is fixed by construction BEFORE any scoring and sealed in
the manifest (``planted_order`` with a hash):

    original  >  proof_removed  >  contradicted  >  irrelevant

* ``original``      the honest concept text;
* ``proof_removed`` the same with the sentences that carry proof/evidence/approval/receipt removed;
* ``contradicted``  the original plus a claim that contradicts it;
* ``irrelevant``    an offer for something unrelated to the persona's needs.

This is a CONSTRUCT-VALIDITY check of the method, not human ground truth: the planted order is the builder's
hypothesis, ``original > proof_removed`` in particular is a judgement, and agreement with it does not show that the
method predicts real buyers. It does measure whether a method can tell obviously worse concepts from the original
at all, how large the margin is relative to noise, and where it fails.
"""
from __future__ import annotations

import dataclasses
import itertools
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import stats
from .contracts import ConceptVariant, StudyResult, stable_hash

PLANTED_KINDS = ("original", "proof_removed", "contradicted", "irrelevant")      # best -> worst, by construction


def build_planted(base: ConceptVariant, *, proof_sentences: Sequence[str], contradiction: str, irrelevant_headline: str,
                  irrelevant_body: str, prefix: str = "P") -> List[ConceptVariant]:
    """``[original, proof_removed, contradicted, irrelevant]`` with ids ``<prefix>1..4``. ``proof_sentences`` are removed
    verbatim from headline/body/CTA (they must occur, otherwise the construction is refused)."""
    def strip(text: str) -> str:
        for s in proof_sentences:
            text = text.replace(s, "")
        return " ".join(text.split())
    whole = " ".join((base.headline, base.body, base.cta))
    missing = [s for s in proof_sentences if s not in whole]
    if missing:
        raise ValueError(f"proof sentences not present in the concept: {missing}")
    common = dict(source_snapshot_hashes=base.source_snapshot_hashes, claims_status=dict(base.claims_status))
    return [
        dataclasses.replace(base, variant_id=f"{prefix}1", label="planted: original", is_control=False, control_kind=""),
        ConceptVariant(variant_id=f"{prefix}2", label="planted: proof/receipts removed", headline=strip(base.headline),
                       body=strip(base.body), cta=strip(base.cta), **common),
        ConceptVariant(variant_id=f"{prefix}3", label="planted: contradicted", headline=base.headline,
                       body=(base.body + " " + contradiction).strip(), cta=base.cta, **common),
        ConceptVariant(variant_id=f"{prefix}4", label="planted: irrelevant offer", headline=irrelevant_headline,
                       body=irrelevant_body, cta="Learn more", **common),
    ]


def seal_planted_order(ids: Sequence[str], variants: Sequence[ConceptVariant], *, note: str = "") -> Dict[str, Any]:
    """Record for the manifest; the hash covers the order AND the content hashes, so it cannot be changed after scoring."""
    by = {v.variant_id: v for v in variants}
    body = {"kinds": list(PLANTED_KINDS), "ids_best_to_worst": list(ids), "content_hashes": {i: by[i].content_hash for i in ids},
            "rule": "fixed by construction before scoring; a hypothesis of the builder, not human ground truth", "note": note}
    return {**body, "sealed_hash": stable_hash(body)}


def recovery(means: Mapping[str, float], ids: Sequence[str]) -> Dict[str, Any]:
    """Agreement of a method's variant means with the planted order (ids best -> worst)."""
    truth = [float(len(ids) - k) for k in range(len(ids))]
    got = [means[i] for i in ids]
    pairs = list(itertools.combinations(range(len(ids)), 2))
    wins = sum(1.0 if got[a] > got[b] else 0.5 if got[a] == got[b] else 0.0 for a, b in pairs)
    return {"kendall_tau": stats.kendall_tau(truth, got), "pairwise_accuracy": wins / len(pairs),
            "top_is_original": max(means, key=means.get) == ids[0], "bottom_is_irrelevant": min(means, key=means.get) == ids[-1],
            "means": {i: means[i] for i in ids}}


def recovery_bootstrap(values: Mapping[str, Sequence[float]], ids: Sequence[str], *, n_boot: int = 1000, seed: int = 0) -> Dict[str, Any]:
    """Bootstrap over PERSONAS (paired across variants): ``values[variant_id][i]`` is persona i's value."""
    import random
    n = len(values[ids[0]])
    if n < 3 or any(len(values[i]) != n for i in ids):
        raise ValueError("need >= 3 paired personas")
    rng = random.Random(seed)
    taus, accs = [], []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        r = recovery({i: stats.mean([values[i][k] for k in idx]) for i in ids}, ids)
        taus.append(r["kendall_tau"])
        accs.append(r["pairwise_accuracy"])
    taus = sorted(t for t in taus if t == t)
    accs.sort()
    point = recovery({i: stats.mean(values[i]) for i in ids}, ids)
    return {"n_personas": n, **point, "tau_ci95": [stats.percentile(taus, 0.025), stats.percentile(taus, 0.975)],
            "accuracy_ci95": [stats.percentile(accs, 0.025), stats.percentile(accs, 0.975)],
            "pairwise_margin": {f"{a}>{b}": stats.mean([x - y for x, y in zip(values[a], values[b])]) for a, b in itertools.combinations(ids, 2)}}


def persona_values(result: StudyResult, construct: str, arm: str, persona_ids: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, float]]:
    """Rebuild ``{variant_id: {persona_id: value}}`` for an arm (``ssr_paper``/``ssr_half``/.../``dlr``/``flr``)."""
    out: Dict[str, Dict[str, float]] = {}
    if arm in ("dlr", "flr"):
        for a in result.arm_ratings:
            if a["arm"] == arm and a["construct"] == construct and a["rating"] is not None:
                out.setdefault(a["variant_id"], {})[a["persona_id"]] = float(a["rating"])
    else:
        resp = {r.response_id: r for r in result.responses if r.arm == "elicitation"}
        for d in result.distributions:
            if d.arm == arm and d.construct == construct:
                r = resp[d.response_id]
                out.setdefault(r.variant_id, {})[r.persona_id] = d.mean
    if persona_ids is not None:
        keep = set(persona_ids)
        out = {v: {p: x for p, x in d.items() if p in keep} for v, d in out.items()}
    return out


def paired(values: Mapping[str, Mapping[str, float]], ids: Sequence[str]) -> Dict[str, List[float]]:
    """Restrict to personas that have a value for EVERY id; returns aligned lists."""
    if any(i not in values for i in ids):
        return {}
    common = sorted(set.intersection(*[set(values[i]) for i in ids]))
    return {i: [values[i][p] for p in common] for i in ids} if len(common) >= 3 else {}


__all__ = ["PLANTED_KINDS", "build_planted", "seal_planted_order", "recovery", "recovery_bootstrap", "persona_values", "paired"]
