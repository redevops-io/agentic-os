"""Held-out split and neutral-labelled stimulus packs for a planned blinded human pilot.

* :func:`split_concepts` divides a concept set into a TUNING half and a HELD-OUT half by a seeded, stratified
  draw BEFORE any scoring; the draw (seed, rule, members) is recorded in the manifest. Settings (embedder, anchor
  pack, epsilon/T, prompts) may be chosen on the tuning half only; the held-out half is not to be inspected for
  tuning.
* :func:`export_stimulus_pack` writes concepts as neutral stimuli: random codes instead of variant labels, optional
  brand removal, seeded presentation order, no variant ids and no synthetic result in the pack. The unblinding key
  is returned separately and must be held by someone who is not coding or analysing human data.
* :func:`seal_hash` hashes the synthetic results (manifest, aggregates, response texts) so the hash can be
  committed before any human data exists.

None of this contacts anyone: it only builds data structures.
"""
from __future__ import annotations

import hashlib
import random
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import ConceptVariant, StudyResult, stable_hash
from .elicitation import ExposureConfig, concept_text

SPLIT_RULE = "stratified-by-group seeded shuffle; first ceil(n/2) of each group = tuning, rest = held-out"


def split_concepts(groups: Mapping[str, Sequence[str]], *, seed: int, held_out_fraction: float = 0.5) -> Dict[str, Any]:
    """``groups``: group id (e.g. niche) -> concept keys in that group. Every group contributes to both halves when it
    has at least two concepts. Deterministic given the seed; returns plain data for the manifest."""
    tuning: List[str] = []
    held: List[str] = []
    for g in sorted(groups):
        keys = sorted(groups[g])
        random.Random(f"{seed}|split|{g}").shuffle(keys)
        n_held = int(round(len(keys) * held_out_fraction))
        if len(keys) >= 2:
            n_held = min(max(n_held, 1), len(keys) - 1)
        held += keys[len(keys) - n_held:]
        tuning += keys[:len(keys) - n_held]
    return {"seed": seed, "rule": SPLIT_RULE, "held_out_fraction": held_out_fraction, "tuning": sorted(tuning),
            "held_out": sorted(held), "split_hash": stable_hash({"t": sorted(tuning), "h": sorted(held), "seed": seed})}


def export_stimulus_pack(concepts: Sequence[Tuple[str, ConceptVariant]], *, seed: int,
                         exposure: ExposureConfig = ExposureConfig(), strip_brands: Sequence[str] = ()
                         ) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """``concepts``: ``[(concept_key, ConceptVariant)]``. Returns ``(pack, unblinding_key)``. The pack has no keys,
    labels or variant ids; ``strip_brands`` names are replaced by ``[the company]`` where they occur."""
    rng = random.Random(f"{seed}|stimuli")
    items = list(concepts)
    rng.shuffle(items)
    stim: List[Dict[str, str]] = []
    key: Dict[str, str] = {}
    for k, v in items:
        text = concept_text(v, exposure)
        for b in strip_brands:
            text = text.replace(b, "[the company]")
        code = "S-" + hashlib.sha256(f"{seed}|{k}".encode()).hexdigest()[:8]
        stim.append({"stimulus_id": code, "text": text})
        key[code] = k
    pack = {"kind": "stimulus_pack", "neutral_labels": True, "seed": seed, "n": len(stim), "stimuli": stim,
            "note": "synthetic-study stimuli for a blinded human pilot; no variant labels or results included"}
    pack["pack_hash"] = stable_hash(pack)
    return pack, key


def seal_hash(result: StudyResult) -> str:
    """sha256 over the synthetic results (manifest, aggregates, comparisons, controls, response texts)."""
    return stable_hash({"manifest": result.manifest, "cells": result.cells, "comparisons": result.comparisons,
                        "controls": result.controls, "responses": [(r.response_id, r.prompt_hash, r.text, r.status) for r in result.responses]})


__all__ = ["SPLIT_RULE", "split_concepts", "export_stimulus_pack", "seal_hash"]
