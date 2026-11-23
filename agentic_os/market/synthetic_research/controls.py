"""Positive/negative controls for a study, with explicit expectations, evaluated with paired bootstrap.

A synthetic panel that cannot pass these should not be believed on anything else. Each control records its
expectation, what was observed, and a pass/fail/None (None = descriptive only). Pass criteria thresholds are
HYPOTHETICAL and shown in every report. Control variants are built from a BASE variant so that only the
manipulated element differs.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import stats
from .contracts import ConceptVariant

FALSE_CLAIM_SENTENCE = ("Independently certified to SOC 2 Type II and ISO 27001, with a guaranteed tenfold revenue increase "
                        "within 30 days and a 99.999% uptime guarantee.")
SKEPTIC = re.compile(r"\b(?:skeptic\w*|doubt\w*|suspicious|too good|unrealistic|can't believe|cannot believe|prove|proof|evidence|"
                     r"verify|verif\w*|audit report|certificate|independent|not convinced|hard to believe|red flag|claims? (?:are|is)|"
                     r"would (?:need|want) to see|need to see|show me)\b", re.I)

CONTROL_KINDS = ("empty_offer", "weak_value", "contradictory", "identical_relabelled", "adversarial_positive", "false_claim")


def build_controls(base: ConceptVariant, *, false_claim: str = FALSE_CLAIM_SENTENCE) -> List[ConceptVariant]:
    """Control variants derived from ``base``; ids ``ctrl_<kind>``; labels are for analysts only."""
    common = dict(source_snapshot_hashes=base.source_snapshot_hashes, is_control=True)
    return [
        ConceptVariant(variant_id="ctrl_empty_offer", label="control: empty offer", headline="Welcome",
                       body="Lorem ipsum dolor sit amet. Click here to learn more.", cta="Click here",
                       control_kind="empty_offer", **common),
        ConceptVariant(variant_id="ctrl_weak_value", label="control: weak value proposition", headline="We are a company",
                       body="We offer services and solutions for your needs. Contact us to find out more.", cta="Contact us",
                       control_kind="weak_value", **common),
        ConceptVariant(variant_id="ctrl_contradictory", label="control: contradictory claims", headline="Free forever. Costs 5,000 dollars a month.",
                       body="Works with every system and works with no system. Unlimited users, maximum one user.", cta=base.cta,
                       control_kind="contradictory", **common),
        replace(base, variant_id="ctrl_identical_relabelled", label="control: identical to base, different label",
                is_control=True, control_kind="identical_relabelled"),
        replace(base, variant_id="ctrl_adversarial_positive", label="control: adversarially positive wording",
                body=(base.body + " " if base.body else "") + "The world's best, revolutionary, perfect solution, 100% guaranteed and "
                "completely risk-free, that every business must have right now!", is_control=True, control_kind="adversarial_positive"),
        replace(base, variant_id="ctrl_false_claim", label="control: deliberately false product claim",
                body=(base.body + " " if base.body else "") + false_claim, is_control=True, control_kind="false_claim"),
    ]


def unpaired_diff_ci(a: Sequence[float], b: Sequence[float], *, n_boot: int = 1000, seed: int = 0) -> Tuple[float, List[float]]:
    import random
    rng = random.Random(seed)
    ds = []
    for _ in range(n_boot):
        ma = stats.mean([a[rng.randrange(len(a))] for _ in a])
        mb = stats.mean([b[rng.randrange(len(b))] for _ in b])
        ds.append(ma - mb)
    ds.sort()
    return stats.mean(a) - stats.mean(b), [stats.percentile(ds, 0.025), stats.percentile(ds, 0.975)]


def paired_diff(base: Mapping[str, float], ctrl: Mapping[str, float], *, n_boot: int = 1000, seed: int = 0) -> Optional[Dict[str, Any]]:
    ids = sorted(set(base) & set(ctrl))
    if len(ids) < 3:
        return None
    r = stats.bootstrap_means({"base": [base[i] for i in ids], "ctrl": [ctrl[i] for i in ids]}, n_boot=n_boot, seed=seed)
    g = r["groups"]["ctrl"]
    return {"n_paired": len(ids), "base_mean": r["groups"]["base"]["mean"], "control_mean": g["mean"],
            "diff": g["diff_vs_first"], "diff_ci95": g["diff_ci95"]}


def evaluate_controls(values: Mapping[Tuple[str, str], Mapping[str, float]], *, base_id: str,
                      false_claim_rows: Optional[Sequence[Mapping[str, Any]]] = None,
                      unseen_values: Optional[Mapping[str, float]] = None, seed: int = 0,
                      identical_tolerance: float = 0.15, n_boot: int = 1000) -> List[Dict[str, Any]]:
    """``values[(variant_id, construct)][persona_id]`` = per-persona SSR mean. Returns one record per control."""
    out: List[Dict[str, Any]] = []

    def get(v: str, c: str) -> Mapping[str, float]:
        return values.get((v, c), {})

    def rec(name, construct, expectation, test):
        d = paired_diff(get(base_id, construct), get(f"ctrl_{name}", construct), n_boot=n_boot, seed=seed)
        if d is None:
            out.append({"control": name, "construct": construct, "expectation": expectation, "observed": None, "passed": None,
                        "note": "too few paired personas", "synthetic": True})
            return
        out.append({"control": name, "construct": construct, "expectation": expectation, "observed": d,
                    "passed": bool(test(d)), "synthetic": True})

    for c in ("purchase_intent", "trust"):
        rec("empty_offer", c, "scores LOWER than the base concept (CI of the difference entirely below 0)", lambda d: d["diff_ci95"][1] < 0)
        rec("weak_value", c, "scores LOWER than the base concept", lambda d: d["diff_ci95"][1] < 0)
        rec("identical_relabelled", c, f"scores the SAME as the base within noise (CI contains 0 and |diff| < {identical_tolerance})",
            lambda d: d["diff_ci95"][0] <= 0 <= d["diff_ci95"][1] and abs(d["diff"]) < identical_tolerance)
    rec("contradictory", "trust", "trust LOWER than the base concept", lambda d: d["diff_ci95"][1] < 0)
    rec("contradictory", "purchase_intent", "purchase intent LOWER than the base concept", lambda d: d["diff_ci95"][1] < 0)
    rec("adversarial_positive", "trust", "hype must NOT raise trust (CI lower bound <= 0)", lambda d: d["diff_ci95"][0] <= 0)
    rec("adversarial_positive", "purchase_intent", "hype must NOT significantly raise purchase intent (CI lower bound <= 0)",
        lambda d: d["diff_ci95"][0] <= 0)
    rec("false_claim", "trust", "trust LOWER than the base concept (descriptive; see skepticism and acceptance below)",
        lambda d: d["diff_ci95"][1] < 0)
    if false_claim_rows:
        n = len(false_claim_rows)
        sk = sum(1 for r in false_claim_rows if r["skeptical"]) / n
        acc = sum(1 for r in false_claim_rows if r["accepts_claim_as_fact"]) / n
        out.append({"control": "false_claim", "construct": "trust", "expectation": "met with skepticism or abstention: "
                    "skeptical share >= 0.5 and uncritical acceptance share <= 0.25 (HYPOTHETICAL thresholds)",
                    "observed": {"n": n, "skeptical_share": sk, "accepts_claim_as_fact_share": acc},
                    "passed": bool(sk >= 0.5 and acc <= 0.25), "synthetic": True})
    if unseen_values is not None and get(base_id, "relevance"):
        a = list(unseen_values.values())
        b = list(get(base_id, "relevance").values())
        if a and b:
            diff, ci = unpaired_diff_ci(a, b, seed=seed, n_boot=n_boot)
            out.append({"control": "unseen_niche", "construct": "relevance",
                        "expectation": "relevance for an out-of-frame niche is LOWER than for in-frame personas (unpaired CI below 0)",
                        "observed": {"n_unseen": len(a), "n_in_frame": len(b), "unseen_mean": stats.mean(a),
                                     "in_frame_mean": stats.mean(b), "diff": diff, "diff_ci95": ci},
                        "passed": bool(ci[1] < 0), "synthetic": True})
    return out


def skeptical(text: str) -> bool:
    return bool(SKEPTIC.search(text))


__all__ = ["FALSE_CLAIM_SENTENCE", "CONTROL_KINDS", "build_controls", "evaluate_controls", "paired_diff",
           "unpaired_diff_ci", "skeptical"]
