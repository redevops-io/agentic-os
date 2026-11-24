"""The study runner: persona x variant x construct -> elicit -> embed -> SSR -> aggregate -> compare -> controls.

READ-ONLY and synthetic-only. The runner has no write path to any website, queue, publisher, outcome store or
ledger; it reads no credentials and no environment secrets (the single environment read is the opt-in flag); it
only calls its injected collaborators (a :class:`~elicitation.Transport` without tools, a
:class:`~embedder.TextEmbedder`). With the feature flag OFF nothing runs.

What is implemented here, kept apart on purpose (see docs/SYNTHETIC_RESEARCH.md):
(i)   published equations: SSR eq. 7-9 with min-subtraction, normalisation and the mean over sets (ssr.py);
(ii)  independently constructed material: the local anchor packs (the paper's six sets are not published);
(iii) everything else is new here: personas, prompts, controls, DLR/FLR comparison arms, themes, groundedness,
      committee model, bootstrap comparisons. Nothing is claimed to replicate the paper or its reported figures.
"""
from __future__ import annotations

import hashlib
import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import FLAG_ENV, enabled, stats
from .anchors import get_pack
from .contracts import (CONSTRUCTS, DISCLAIMERS, FIDELITY_STATEMENT, ConceptVariant, EmbedderMismatch, PersonaSpec,
                        SourceSnapshot, SSRDistribution, StudyManifest, StudyResult, SyntheticResponse, SyntheticViolation,
                        stable_hash)
from .controls import FALSE_CLAIM_SENTENCE, build_controls, evaluate_controls, skeptical
from .elicitation import (PROMPT_TEMPLATE_VERSION, BudgetExceeded, Elicitor, ExposureConfig, GenerationParams, Job,
                          StudyBudget, Transport, build_messages, concept_text, describe_persona, estimate_tokens, parse_rating)
from .groundedness import find_unsupported
from .pipeline import embed_anchor_pack
from .sanitize import detect_injection
from .ssr import VARIANT_PAPER, VARIANT_REFERENCE, ssr_score
from .themes import extract_themes, summarise_themes

LIMITATIONS = (
    "Respondents are language models role-playing archetypes; they are not people and carry model biases.",
    "SSR maps text to a 1-5 scale with a local anchor pack and a general embedding model; the scale compresses "
    "toward the middle and has not been validated against human purchase intent for these niches.",
    "Rankings are conditional on the persona frame, the variants, the prompts, the models and the embedder; "
    "differences inside the reported intervals should be treated as noise.",
    "Intervals are bootstrap intervals over synthetic personas: they quantify sampling noise of this panel only, "
    "not uncertainty about real buyers.",
    "Price scenarios are hypotheses, not measured willingness to pay.",
    "Nothing here measures or predicts conversion, demand or search ranking.",
)

PRIMARY_ARM = ("ssr_paper", VARIANT_PAPER, 0.0, 1.0)
SWEEP_ARMS = (("ssr_half", VARIANT_REFERENCE, 0.0, 1.0), ("ssr_paper_T0.5", VARIANT_PAPER, 0.0, 0.5),
              ("ssr_paper_T2", VARIANT_PAPER, 0.0, 2.0), ("ssr_paper_eps0.05", VARIANT_PAPER, 0.05, 1.0),
              ("ssr_half_eps0.05", VARIANT_REFERENCE, 0.05, 1.0))


class SyntheticResearchDisabled(RuntimeError):
    """The feature flag is OFF: nothing may run."""


def require_enabled() -> None:
    if not enabled():
        raise SyntheticResearchDisabled(f"synthetic research is OFF (set {FLAG_ENV}=1 to opt in); nothing was run")


@dataclass(frozen=True)
class StudyConfig:
    study_id: str
    kind: str = "custom"                         # sme | enterprise | custom
    seed: int = 20261122
    constructs: Tuple[str, ...] = ("purchase_intent", "trust")
    params: GenerationParams = GenerationParams()
    budget: StudyBudget = StudyBudget()
    exposure: ExposureConfig = ExposureConfig()
    concurrency: int = 2
    arm_constructs: Tuple[str, ...] = ()         # constructs that also get the DLR and FLR comparison arms
    run_controls: bool = True
    control_personas_per_cell: int = 4
    control_constructs: Tuple[str, ...] = ("purchase_intent", "trust")
    control_base_id: str = ""                    # defaults to the first variant
    # Draws per persona x variant x construct, averaged per persona before any comparison. One draw at a sampling temperature above zero is
    # one sample of a noisy answer: the identical-text control then differs from its own base by chance (measured 2026-11-24: +0.22 and
    # -0.31 on a five-point scale with one draw each, on a server that ignores `seed`). More draws shrink that noise; they do not add personas.
    replicates: int = 1
    n_boot: int = 1000
    sweep: bool = True
    model_id: str = ""
    model_info: Mapping[str, Any] = field(default_factory=dict)     # e.g. {"label": ..., "route_receipt": {...}}
    false_claim: str = FALSE_CLAIM_SENTENCE
    frame_hashes: Mapping[str, str] = field(default_factory=dict)
    code_version: str = ""
    planted_order: Mapping[str, Any] = field(default_factory=dict)    # sealed BEFORE scoring (planted.seal_planted_order)
    false_claim_pattern: str = r"soc\s?2|iso\s?27001|tenfold|10x|99\.999|uptime guarantee"
    concept_split: Mapping[str, Any] = field(default_factory=dict)    # from stimuli.split_concepts, drawn BEFORE scoring


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wmean(xs: Sequence[float], ws: Sequence[float]) -> float:
    t = sum(ws)
    return sum(x * w for x, w in zip(xs, ws)) / t if t else float("nan")


def _hist_pmf(vals: Sequence[int]) -> List[float]:
    c = Counter(vals)
    n = len(vals)
    return [c.get(i, 0) / n for i in range(1, 6)]


def _pooled_entropy(arm, construct, vals_all, primary_pmf, rid_to_resp, variant_ids):
    if arm != "ssr_paper":
        return stats.entropy(_hist_pmf([int(round(x)) for x in vals_all]))
    tot = [0.0] * 5
    for rid, pmf in primary_pmf.items():
        r = rid_to_resp[rid]
        if r.construct == construct and r.variant_id in variant_ids:
            for i in range(5):
                tot[i] += pmf[i]
    t = sum(tot)
    return stats.entropy([x / t for x in tot]) if t else float("nan")


def run_study(config: StudyConfig, *, personas: Sequence[PersonaSpec], variants: Sequence[ConceptVariant],
              snapshots: Sequence[SourceSnapshot], transport: Transport, embedder: Any,
              control_personas: Sequence[PersonaSpec] = (), unseen_personas: Sequence[PersonaSpec] = (),
              vector_sink: Optional[Dict[str, List[float]]] = None) -> StudyResult:
    require_enabled()                                         # flag OFF => nothing runs, no collaborator is touched
    for c in config.constructs:
        if c not in CONSTRUCTS:
            raise ValueError(f"unknown construct {c!r}")
    if not personas or not variants:
        raise ValueError("a study needs personas and variants")
    if not 1 <= int(config.replicates) <= 10:
        raise ValueError("replicates must be 1 to 10")
    if len({p.persona_id for p in personas}) != len(personas):
        raise ValueError("duplicate persona ids")
    if len({v.variant_id for v in variants}) != len(variants):
        raise ValueError("duplicate variant ids")
    t_wall = time.monotonic()
    base_id = config.control_base_id or variants[0].variant_id
    base = next(v for v in variants if v.variant_id == base_id)
    snap_by_hash = {s.sha256: s for s in snapshots}
    vmap: Dict[str, ConceptVariant] = {v.variant_id: v for v in variants}
    controls: List[ConceptVariant] = build_controls(base, false_claim=config.false_claim) if config.run_controls else []
    for v in controls:
        vmap[v.variant_id] = v
    pmap: Dict[str, PersonaSpec] = {p.persona_id: p for p in list(personas) + list(control_personas) + list(unseen_personas)}

    # ── plan: jobs in a per-persona randomised variant order ───────────────────────────────────────────────
    jobs: List[Job] = []

    def add(plist: Sequence[PersonaSpec], vlist: Sequence[ConceptVariant], constructs: Sequence[str], tag: str) -> None:
        for p in plist:
            order = list(vlist)
            random.Random(f"{config.seed}|{tag}|{p.persona_id}").shuffle(order)       # order randomisation
            for v in order:
                for c in constructs:
                    for k in range(config.replicates):
                        jobs.append(Job(p, v, c, len(jobs), "elicitation", config.exposure, replicate=k))

    add(personas, variants, config.constructs, "main")
    if controls:
        by_cell: Dict[str, List[PersonaSpec]] = defaultdict(list)
        for p in (control_personas or personas):
            by_cell[p.cell].append(p)
        cp = [p for cell in sorted(by_cell) for p in by_cell[cell][:config.control_personas_per_cell]]
        add(cp, controls, config.control_constructs, "ctrl")
        if unseen_personas:
            add(unseen_personas, [base], ("relevance",), "unseen")
            if "relevance" not in config.constructs:
                add(cp, [base], ("relevance",), "unseen_ref")      # in-frame reference for the unseen-niche control
    n_main = len(jobs)
    dlr_jobs: List[Job] = []
    for c in config.arm_constructs:
        for p in personas:
            for v in variants:
                dlr_jobs.append(Job(p, v, c, len(jobs) + len(dlr_jobs), "dlr", config.exposure))
    n_flr = len(personas) * len(variants) * len(config.arm_constructs) * config.replicates

    # ── budget: refused BEFORE any model call ────────────────────────────────────────────────────────────────
    el = Elicitor(transport, config.model_id, config.params, study_seed=config.seed, concurrency=config.concurrency,
                  max_wall_seconds=config.budget.max_wall_seconds, max_total_tokens=config.budget.max_total_tokens)
    prompts = [el.messages_for(j)[0] for j in jobs + dlr_jobs]
    est = estimate_tokens(prompts, config.params) + n_flr * (150 + 6)
    total_calls = len(jobs) + len(dlr_jobs) + n_flr
    config.budget.check(n_personas=len(personas), n_variants=len(variants), n_constructs=len(config.constructs),
                        total_calls=total_calls, est_tokens=est, params=config.params)

    # ── untrusted-content flags (page/variant text BEFORE neutralisation) ────────────────────────────────────
    flag_counts: Counter = Counter()
    snap_flags: Dict[str, Dict[str, int]] = {}
    for s in snapshots:
        fl = detect_injection(s.text)
        if fl:
            snap_flags[s.url] = {f.kind: f.count for f in fl}
            flag_counts.update({f.kind: f.count for f in fl})
    var_flags: Dict[str, Dict[str, int]] = {}
    for v in list(variants) + controls:
        fl = detect_injection(concept_text(v, ExposureConfig(True, True, True)))
        if fl:
            var_flags[v.variant_id] = {f.kind: f.count for f in fl}
            flag_counts.update({f.kind: f.count for f in fl})

    # ── elicitation ──────────────────────────────────────────────────────────────────────────────────────────
    responses: List[SyntheticResponse] = el.run(jobs)
    responses += el.run(dlr_jobs) if dlr_jobs else []
    elic = [r for r in responses if r.arm == "elicitation"]
    # FLR: rate each included elicitation answer for the arm constructs
    flr: List[SyntheticResponse] = []
    for r in elic:
        if r.construct in config.arm_constructs and r.included and not vmap[r.variant_id].is_control:
            rating, res = el.rate_text(r.text, r.construct, key=r.response_id)
            flr.append(SyntheticResponse(
                response_id=f"flr:{r.response_id}", persona_id=r.persona_id, variant_id=r.variant_id, construct=r.construct,
                text="" if rating is None else str(rating), model_id=config.model_id,
                params={"temperature": config.params.temperature, "top_p": config.params.top_p, "max_tokens": 6},
                seed=None, prompt_hash="", prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens,
                latency_s=res.latency_s, finish_reason=res.finish_reason, arm="flr",
                status="ok" if rating is not None else "empty",
                exclude_reason="" if rating is not None else "no 1-5 digit in the answer", order_index=r.order_index))
    responses += flr

    # ── embed + SSR ──────────────────────────────────────────────────────────────────────────────────────────
    inc = [r for r in elic if r.included]
    vecs = embedder.embed([r.text for r in inc]) if inc else []
    if len(vecs) != len(inc):
        raise ValueError("embedder returned the wrong number of vectors")
    if vector_sink is not None:
        vector_sink.update({r.response_id: v for r, v in zip(inc, vecs)})
    pack_ids: Dict[str, str] = {}
    dists: List[SSRDistribution] = []
    primary_mean: Dict[str, float] = {}
    arm_mean: Dict[str, Dict[str, float]] = defaultdict(dict)
    primary_pmf: Dict[str, Tuple[float, ...]] = {}
    degenerate: Dict[str, bool] = {}
    arms = [PRIMARY_ARM] + (list(SWEEP_ARMS) if config.sweep else [])
    for construct in sorted({r.construct for r in inc}):
        pack = get_pack(construct)
        pack_ids[construct] = pack.pack_id
        anchors = embed_anchor_pack(pack, embedder)
        for r, v in zip(inc, vecs):
            if r.construct != construct:
                continue
            for name, variant, eps, temp in arms:
                s = ssr_score(v, anchors, variant=variant, epsilon=eps, temperature=temp)
                arm_mean[name][r.response_id] = s.mean
                if name == PRIMARY_ARM[0]:
                    primary_mean[r.response_id] = s.mean
                    primary_pmf[r.response_id] = s.pmf
                    degenerate[r.response_id] = s.is_degenerate
                dists.append(SSRDistribution(
                    response_id=r.response_id, construct=construct, pmf=s.pmf, mean=s.mean,
                    per_set_pmfs=s.per_set_pmfs if name == PRIMARY_ARM[0] else (), variant=variant, epsilon=eps,
                    temperature=temp, pack_id=pack.pack_id, embedder_name=embedder.name, degenerate_sets=s.degenerate_sets,
                    degenerate=s.is_degenerate, arm=name))

    # ── per-persona values ───────────────────────────────────────────────────────────────────────────────────
    rid_to_resp = {r.response_id: r for r in elic}
    val: Dict[Tuple[str, str, str], Dict[str, float]] = defaultdict(dict)       # (variant, construct, arm) -> persona -> value
    draws: Dict[Tuple[str, str, str], Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for rid, m in primary_mean.items():
        r = rid_to_resp[rid]
        for arm in arm_mean:
            if rid in arm_mean[arm]:
                draws[(r.variant_id, r.construct, arm)][r.persona_id].append(arm_mean[arm][rid])
    for key, by_persona in draws.items():                     # a persona's value is the mean of its included draws (one, by default)
        for pid, xs in by_persona.items():
            val[key][pid] = stats.mean(xs)
    for r in responses:
        if r.arm in ("dlr", "flr") and r.status == "ok":
            n = parse_rating(r.text)
            if n is not None:
                val[(r.variant_id, r.construct, r.arm)][r.persona_id] = float(n)

    main_ids = {p.persona_id for p in personas}
    cell_of = {p.persona_id: p.cell for p in personas}
    variant_ids = [v.variant_id for v in variants]
    label = {v.variant_id: v.label for v in list(variants) + controls}
    cells: List[Dict[str, Any]] = []
    for cell in sorted({p.cell for p in personas}):
        pids = [p.persona_id for p in personas if p.cell == cell]
        for construct in config.constructs:
            for vid in variant_ids:
                rs = [r for r in elic if r.persona_id in pids and r.variant_id == vid and r.construct == construct]
                incl = [r for r in rs if r.included]
                vals = [primary_mean[r.response_id] for r in incl]
                excl = Counter(r.status for r in rs if not r.included)
                pmfs = [primary_pmf[r.response_id] for r in incl]
                half = ("held_out" if f"{cell}|{vid}" in config.concept_split.get("held_out", ()) else
                        "tuning" if f"{cell}|{vid}" in config.concept_split.get("tuning", ()) else "unsplit")
                rec: Dict[str, Any] = {"cell": cell, "construct": construct, "variant_id": vid, "label": label[vid], "split": half,
                                       "n_personas": len(pids), "n_included": len(incl), "excluded": dict(excl),
                                       "synthetic": True}
                if vals:
                    rec.update({"mean_rating": stats.mean(vals), "sd": stats.sd(vals),
                                "se": stats.sd(vals) / (len(vals) ** 0.5) if len(vals) > 1 else None,
                                "mean_pmf": [sum(p[i] for p in pmfs) / len(pmfs) for i in range(5)],
                                "degenerate": sum(1 for r in incl if degenerate[r.response_id]),
                                "arm_means": {a: stats.mean([arm_mean[a][r.response_id] for r in incl]) for a in arm_mean}})
                for a in ("dlr", "flr"):
                    d = [val[(vid, construct, a)][pid] for pid in pids if pid in val.get((vid, construct, a), {})]
                    if d:
                        rec[f"{a}_mean"], rec[f"{a}_sd"], rec[f"{a}_n"] = stats.mean(d), stats.sd(d), len(d)
                cells.append(rec)

    # ── variant comparisons with paired bootstrap, per arm; weight sensitivity ───────────────────────────────
    comparisons: List[Dict[str, Any]] = []
    for cell in sorted({p.cell for p in personas}):
        pids = [p.persona_id for p in personas if p.cell == cell]
        for construct in config.constructs:
            per_arm_rank: Dict[str, Dict[str, float]] = {}
            paired: Optional[List[str]] = None
            for arm in ["ssr_paper"] + [a for a in arm_mean if a != "ssr_paper"] + ["dlr", "flr"]:
                have = [val.get((vid, construct, arm), {}) for vid in variant_ids]
                ids = [pid for pid in pids if all(pid in h for h in have)]
                if len(ids) < 3:
                    continue
                if arm == "ssr_paper":
                    paired = ids
                    boot = stats.bootstrap_means({vid: [val[(vid, construct, arm)][i] for i in ids] for vid in variant_ids},
                                                 n_boot=config.n_boot, seed=config.seed)
                    comparisons.append({
                        "kind": "variants", "cell": cell, "construct": construct, "arm": arm, "n_paired": len(ids),
                        "variants": [{"variant_id": vid, "label": label[vid], **boot["groups"][vid]} for vid in variant_ids],
                        "ranking": sorted(variant_ids, key=lambda k: -boot["groups"][k]["mean"]), "synthetic": True})
                per_arm_rank[arm] = {vid: stats.mean([val[(vid, construct, arm)][i] for i in ids]) for vid in variant_ids}
            if "ssr_paper" in per_arm_rank:
                prim = per_arm_rank["ssr_paper"]
                arms_rec = {}
                for a, m in per_arm_rank.items():
                    ks = sorted(m)
                    fl, pairs, flipped = stats.pairwise_flips(prim, m)
                    arms_rec[a] = {"means": m, "ranking": sorted(m, key=lambda k: -m[k]),
                                   "spread": max(m.values()) - min(m.values()),
                                   "spearman_vs_ssr_paper": stats.spearman([prim[k] for k in ks], [m[k] for k in ks]),
                                   "kendall_vs_ssr_paper": stats.kendall_tau([prim[k] for k in ks], [m[k] for k in ks]),
                                   "pairwise_flips": fl, "comparable_pairs": pairs}
                # sensitivity to hypothetical weights (post-stratification by firm size / hypothetical weight)
                ws = {}
                if paired:
                    for sname, wf in (("plain", lambda p: 1.0), ("hypothetical_weight", lambda p: p.hypothetical_weight or 1.0),
                                      ("firm_size_uniform", None)):
                        means = {}
                        for vid in variant_ids:
                            xs = [val[(vid, construct, "ssr_paper")][i] for i in paired]
                            if wf is None:
                                grp: Dict[str, List[float]] = defaultdict(list)
                                for i, x in zip(paired, xs):
                                    grp[pmap[i].firm_size].append(x)
                                means[vid] = stats.mean([stats.mean(g) for g in grp.values()])
                            else:
                                means[vid] = _wmean(xs, [wf(pmap[i]) for i in paired])
                        ws[sname] = {"means": means, "ranking": sorted(means, key=lambda k: -means[k])}
                comparisons.append({"kind": "arms", "cell": cell, "construct": construct, "arms": arms_rec,
                                    "weight_sensitivity": ws,
                                    "weight_ranking_changes": len({tuple(v["ranking"]) for v in ws.values()}) > 1,
                                    "synthetic": True})
    # pooled per-arm compression summary (the paper's claim that direct ratings regress to the middle)
    for construct in config.arm_constructs:
        for arm in ["ssr_paper", "dlr", "flr"]:
            vals_all = [x for (vid, c, a), d in val.items() if c == construct and a == arm and vid in variant_ids for x in d.values()]
            if not vals_all:
                continue
            spreads = [rec["arms"][arm]["spread"] for rec in comparisons if rec["kind"] == "arms" and rec["construct"] == construct and arm in rec["arms"]]
            comparisons.append({
                "kind": "arm_summary", "construct": construct, "arm": arm, "n": len(vals_all), "mean": stats.mean(vals_all),
                "sd": stats.sd(vals_all), "mean_abs_dev_from_3": stats.mean([abs(x - 3) for x in vals_all]),
                "share_within_0.5_of_3": sum(1 for x in vals_all if abs(x - 3) <= 0.5) / len(vals_all),
                "mean_between_variant_spread": stats.mean(spreads) if spreads else None,
                "pooled_pmf_entropy_bits": _pooled_entropy(arm, construct, vals_all, primary_pmf, rid_to_resp, variant_ids),
                "synthetic": True})

    # ── themes and groundedness ──────────────────────────────────────────────────────────────────────────────
    theme_recs = extract_themes([r for r in elic if r.persona_id in main_ids or vmap[r.variant_id].is_control], vmap)
    n_resp = Counter((r.variant_id, r.construct) for r in elic if r.included)
    theme_summary = summarise_themes(theme_recs, n_responses=n_resp)
    ground: List[Dict[str, Any]] = []
    false_rows: List[Dict[str, Any]] = []
    import re as _re
    claim_re = _re.compile(config.false_claim_pattern, _re.I)
    for r in inc:
        v = vmap[r.variant_id]
        snap_text = [snap_by_hash[h].text for h in v.source_snapshot_hashes if h in snap_by_hash]
        corpus = snap_text + [concept_text(v, ExposureConfig(True, True, True)), describe_persona(pmap[r.persona_id])]
        fl = find_unsupported(r.text, corpus)
        if fl:
            ground.append({"response_id": r.response_id, "variant_id": r.variant_id, "construct": r.construct,
                           "flags": [{"kind": f.kind, "text": f.text, "sentence": f.sentence} for f in fl], "synthetic": True})
        if v.control_kind == "false_claim":
            fl2 = find_unsupported(r.text, snap_text + [describe_persona(pmap[r.persona_id])])
            false_rows.append({"response_id": r.response_id, "skeptical": skeptical(r.text),
                               "accepts_claim_as_fact": any(claim_re.search(f.text) or claim_re.search(f.sentence) for f in fl2)})

    # ── controls ─────────────────────────────────────────────────────────────────────────────────────────────
    ctrl_records: List[Dict[str, Any]] = []
    if controls:
        cvals: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(dict)
        unseen_ids = {p.persona_id for p in unseen_personas}
        cdraws: Dict[Tuple[str, str], Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
        udraws: Dict[str, List[float]] = defaultdict(list)
        for r in inc:
            if r.persona_id not in unseen_ids:
                cdraws[(r.variant_id, r.construct)][r.persona_id].append(primary_mean[r.response_id])
            elif r.variant_id == base_id:
                udraws[r.persona_id].append(primary_mean[r.response_id])
        for key, by_persona in cdraws.items():
            for pid, xs in by_persona.items():
                cvals[key][pid] = stats.mean(xs)
        # only personas that actually have control responses count as paired
        unseen_vals = {pid: stats.mean(xs) for pid, xs in udraws.items()}
        ctrl_records = evaluate_controls(cvals, base_id=base_id, false_claim_rows=false_rows,
                                         unseen_values=unseen_vals or None, seed=config.seed, n_boot=config.n_boot)

    # ── abstentions ──────────────────────────────────────────────────────────────────────────────────────────
    excl_all = Counter(r.status for r in elic if not r.included)
    abst = {"elicitation_calls": len(elic), "included": len(inc), "excluded_by_status": dict(excl_all),
            "exclusion_rate": (len(elic) - len(inc)) / len(elic) if elic else 0.0,
            "dlr_unparsed": sum(1 for r in responses if r.arm == "dlr" and r.status != "ok"),
            "flr_unparsed": sum(1 for r in responses if r.arm == "flr" and r.status != "ok"),
            "degenerate_ssr_responses": sum(1 for v in degenerate.values() if v),
            "by_variant": {vid: dict(Counter(r.status for r in elic if r.variant_id == vid and not r.included))
                           for vid in list(variant_ids) + [c.variant_id for c in controls]}}
    flag_counts.update({f"response_quarantined:{k}": n for k, n in
                        Counter(r.exclude_reason for r in elic if r.status == "quarantined").items()})
    untrusted = {"snapshots": snap_flags, "variants": var_flags,
                 "responses_quarantined": dict(Counter(r.exclude_reason for r in elic if r.status == "quarantined")),
                 "totals_by_kind": dict(flag_counts),
                 "handling": "page text is data: neutralised, delimited, no tools/browsing/files/network for the model; "
                             "output parsed as text only"}

    # ── manifest ─────────────────────────────────────────────────────────────────────────────────────────────
    wall = time.monotonic() - t_wall
    cost = {"calls": len(responses), "prompt_tokens": sum(r.prompt_tokens for r in responses),
            "completion_tokens": sum(r.completion_tokens for r in responses),
            "model_latency_s_sum": round(sum(r.latency_s for r in responses), 2), "wall_clock_s": round(wall, 2),
            "concurrency": el.concurrency, "estimated_tokens_before_run": est}
    manifest = StudyManifest(
        study_id=config.study_id, kind=config.kind, created_at=_now(), seed=config.seed,
        models=({"model_id": config.model_id, **dict(config.model_info)},),
        generation_params={"temperature": config.params.temperature, "top_p": config.params.top_p,
                           "max_tokens": config.params.max_tokens, "enable_thinking": config.params.enable_thinking,
                           "seed_passed": config.params.use_seed, "exposure": vars(config.exposure) if hasattr(config.exposure, "__dict__") else {},
                           "seed_policy": "per-call seed = f(study seed, arm, persona, variant content hash, construct)"},
        budget=vars(config.budget).copy() if hasattr(config.budget, "__dict__") else {}, anchor_pack_ids=pack_ids,
        embedder_name=embedder.name, embedder_revision=str(getattr(embedder, "revision", "") or "unrecorded"),
        ssr_arms=tuple({"arm": n, "similarity": v, "epsilon": e, "temperature": t, "primary": n == PRIMARY_ARM[0]} for n, v, e, t in arms),
        frame_hashes=dict(config.frame_hashes), persona_count=len(personas),
        variant_hashes={v.variant_id: v.content_hash for v in list(variants) + controls},
        snapshot_refs=tuple({"url": s.url, "sha256": s.sha256, "fetched_at": s.fetched_at} for s in snapshots),
        constructs=tuple(config.constructs), prompt_template_version=PROMPT_TEMPLATE_VERSION,
        counts={"elicitation_calls": len(elic), "dlr_calls": sum(1 for r in responses if r.arm == "dlr"),
                "flr_calls": len(flr), "control_calls": sum(1 for r in elic if vmap[r.variant_id].is_control),
                "personas": len(personas), "variants": len(variants), "controls": len(controls)},
        cost=cost, concept_split=dict(config.concept_split), planted_order=dict(config.planted_order), untrusted_content_flags=untrusted, governance=dict(config.model_info.get("route_receipt") or {}) if isinstance(config.model_info.get("route_receipt"), Mapping) else {},
        truncated=el.truncated, limitations=LIMITATIONS, code_version=config.code_version)
    return StudyResult(manifest=manifest, personas=tuple(list(personas) + list(control_personas) + list(unseen_personas)),
                       variants=tuple(list(variants) + controls), responses=tuple(responses), distributions=tuple(dists),
                       cells=tuple(cells), comparisons=tuple(comparisons), arm_ratings=tuple(
                           {"response_id": r.response_id, "persona_id": r.persona_id, "variant_id": r.variant_id,
                            "construct": r.construct, "arm": r.arm, "rating": parse_rating(r.text)}
                           for r in responses if r.arm in ("dlr", "flr")),
                       themes=tuple(theme_summary), groundedness=tuple(ground), controls=tuple(ctrl_records), abstentions=abst)


def compare_results(a: StudyResult, b: StudyResult, *, allow_embedder_mismatch: bool = False,
                    arm: str = "ssr_paper") -> Dict[str, Any]:
    """Rank agreement of variants per (cell, construct) between two studies (two models, two seeds, ...).
    Refuses to compare studies with different embedders or revisions unless ``allow_embedder_mismatch=True``,
    in which case the output carries a visible ``embedder_mismatch`` flag."""
    ea = (a.manifest.embedder_name, a.manifest.embedder_revision)
    eb = (b.manifest.embedder_name, b.manifest.embedder_revision)
    mismatch = ea != eb
    if mismatch and not allow_embedder_mismatch:
        raise EmbedderMismatch(f"embedders differ ({ea} vs {eb}): rankings are not comparable without an explicit flag")
    packs_differ = dict(a.manifest.anchor_pack_ids) != dict(b.manifest.anchor_pack_ids)
    index = {(c["cell"], c["construct"]): c for c in b.comparisons if c["kind"] == "arms"}
    rows = []
    for ca in a.comparisons:
        if ca["kind"] != "arms" or arm not in ca["arms"]:
            continue
        cb = index.get((ca["cell"], ca["construct"]))
        if not cb or arm not in cb["arms"]:
            continue
        ma, mb = ca["arms"][arm]["means"], cb["arms"][arm]["means"]
        ks = sorted(set(ma) & set(mb))
        fl, pairs, flipped = stats.pairwise_flips(ma, mb)
        rows.append({"cell": ca["cell"], "construct": ca["construct"], "n_variants": len(ks),
                     "spearman": stats.spearman([ma[k] for k in ks], [mb[k] for k in ks]),
                     "kendall": stats.kendall_tau([ma[k] for k in ks], [mb[k] for k in ks]),
                     "pairwise_flips": fl, "comparable_pairs": pairs, "reversed_pairs": [list(p) for p in flipped],
                     "top_a": max(ma, key=ma.get), "top_b": max(mb, key=mb.get), "top_agree": max(ma, key=ma.get) == max(mb, key=mb.get)})
    return {"study_a": a.manifest.study_id, "study_b": b.manifest.study_id, "embedder_mismatch": mismatch,
            "anchor_pack_mismatch": packs_differ, "rows": rows, "synthetic": True}


__all__ = ["LIMITATIONS", "PRIMARY_ARM", "SWEEP_ARMS", "SyntheticResearchDisabled", "require_enabled", "StudyConfig",
           "run_study", "compare_results"]
