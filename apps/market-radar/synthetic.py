"""Synthetic buyer study inside Market Radar: how a set of message variants reads to synthetic respondents.

Market Radar already answers "what changed on the pages we watch". This adds a second, clearly separate
question an analyst asks next: "given that, which of our own message variants is worth testing with real
buyers?" It runs the synthetic research study (``agentic_os.market.synthetic_research``) over variants the
caller supplies and returns a compact, labelled summary.

What the result is: a hypothesis for a person to review. What it is not: customer research, a prediction of
conversion, or evidence. Every result says so in its own fields (``synthetic``, ``evidence_class``,
``requires_human_review``). The study's built-in controls are judged per construct: where a construct's controls
fail, its ranking is withheld, so a number cannot be quoted from a measure that could not tell an empty offer from a
real one. When no construct passes, the study is ``inconclusive``.

Off unless ``AGENTIC_OS_SYNTHETIC_RESEARCH`` is set. It reads nothing from, and writes nothing to, the watch
store, an outcome store or the ledger; the respondent model and the embedding model must both be inside the
trust boundary (``SYNTHETIC_RESEARCH_LLM_URL`` / ``_LLM_MODEL``, ``SYNTHETIC_RESEARCH_EMBED_URL`` /
``_EMBED_MODEL``), which the study's own transports enforce.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from agentic_os.market import synthetic_research as sr

AUDIENCES = {"sme": ("sme_niches", "segment"), "enterprise": ("enterprise_committee", "cell")}
MAX_VARIANTS, MAX_PER_CELL, MAX_SEGMENTS = 4, 8, 3


def _refused(error: str, **extra: Any) -> dict:
    return {"status": "refused", "action": "synthetic_study", "synthetic_study_run": False, "error": error, **extra}


def collaborators_from_env():
    """(transport, embedder, model id) from the environment, or a ``ValueError`` saying what is missing.

    The embedder is either an in-process sentence-transformers model (``SYNTHETIC_RESEARCH_EMBEDDER=
    sentence-transformers:<model>``, nothing leaves the process) or an in-boundary HTTP embeddings endpoint
    (``SYNTHETIC_RESEARCH_EMBED_URL`` + ``SYNTHETIC_RESEARCH_EMBED_MODEL``)."""
    from agentic_os.market.synthetic_research.elicitation import LocalOpenAITransport  # noqa: PLC0415
    from agentic_os.market.synthetic_research.embedder import HttpEmbedder, SentenceTransformerEmbedder  # noqa: PLC0415
    env = os.environ.get
    missing = [k for k in ("SYNTHETIC_RESEARCH_LLM_URL", "SYNTHETIC_RESEARCH_LLM_MODEL") if not env(k)]
    local = env("SYNTHETIC_RESEARCH_EMBEDDER", "")
    if not local.startswith("sentence-transformers:"):
        missing += [k for k in ("SYNTHETIC_RESEARCH_EMBED_URL", "SYNTHETIC_RESEARCH_EMBED_MODEL") if not env(k)]
    if missing:
        raise ValueError("not configured: " + ", ".join(missing))
    revision = env("SYNTHETIC_RESEARCH_EMBED_REVISION", "unpinned")
    embedder = (SentenceTransformerEmbedder(local.split(":", 1)[1], revision=revision) if local.startswith("sentence-transformers:")
                else HttpEmbedder(env("SYNTHETIC_RESEARCH_EMBED_URL"), env("SYNTHETIC_RESEARCH_EMBED_MODEL"), revision=revision))
    return LocalOpenAITransport(env("SYNTHETIC_RESEARCH_LLM_URL")), embedder, env("SYNTHETIC_RESEARCH_LLM_MODEL")


def synthetic_study(body: dict, *, transport: Any = None, embedder: Any = None, model_id: str = "") -> dict:
    """Run one small study. ``body``: ``variants`` (2-4 of ``{id, headline, body, cta}``), ``audience``
    (``sme`` | ``enterprise``), optional ``segments``, ``scenario`` (enterprise: one per study), ``per_cell`` (1-8;
    per segment for ``sme``, per committee role for ``enterprise``), ``seed``, ``study_id``."""
    if not sr.enabled():
        return {"status": "disabled", "action": "synthetic_study", "synthetic_study_run": False,
                "error": f"synthetic research is off (set {sr.FLAG_ENV}=1 to opt in); nothing was run"}
    from agentic_os.market.synthetic_research import contracts as C, elicitation as E, personas as P, study as SV  # noqa: PLC0415

    raw = body.get("variants")
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_VARIANTS:
        return _refused(f"give 2 to {MAX_VARIANTS} message variants to compare")
    audience = str(body.get("audience") or "sme")
    if audience not in AUDIENCES:
        return _refused(f"audience must be one of {sorted(AUDIENCES)}")
    try:
        per_cell = int(body.get("per_cell", 4))
        seed = int(body.get("seed", 20261123))
    except (TypeError, ValueError):
        return _refused("per_cell and seed must be whole numbers")
    if not 1 <= per_cell <= MAX_PER_CELL:
        return _refused(f"per_cell must be 1 to {MAX_PER_CELL}")
    variants: List[Any] = []
    for i, v in enumerate(raw):
        if not isinstance(v, dict) or not str(v.get("headline") or v.get("body") or "").strip():
            return _refused(f"variant {i + 1} needs a headline or a body")
        vid = re.sub(r"[^A-Za-z0-9_]", "", str(v.get("id") or chr(65 + i)))[:16] or chr(65 + i)
        try:
            variants.append(C.ConceptVariant(variant_id=vid, label=str(v.get("label") or vid), headline=str(v.get("headline") or ""),
                                             body=str(v.get("body") or ""), cta=str(v.get("cta") or "")))
        except ValueError as e:
            return _refused(f"variant {i + 1}: {e}")
    if len({v.variant_id for v in variants}) != len(variants):
        return _refused("variant ids must be distinct")

    frame_name, level = AUDIENCES[audience]
    frame = P.load_frame(frame_name)
    known = [getattr(s, "id", None) or (s.get("id") if isinstance(s, dict) else str(s)) for s in frame.segments]
    segments = [str(s) for s in (body.get("segments") or known[:1])]
    if not segments or len(segments) > MAX_SEGMENTS or not set(segments) <= set(known):
        return _refused(f"segments must be 1 to {MAX_SEGMENTS} of {known}")
    try:
        if transport is None or embedder is None:
            transport, embedder, model_id = collaborators_from_env()
        kw: Dict[str, Any] = {}
        if frame.scenarios:                                       # a buying committee: every role, in ONE scenario per study
            ids = [sc["id"] for sc in frame.scenarios]
            scenario = str(body.get("scenario") or ids[0])
            if scenario not in ids:
                return _refused(f"scenario must be one of {ids}")
            kw["scenarios"] = [scenario]
        personas = P.sample_personas(frame, per_cell, seed, segments=segments, level=level, **kw)
        config = SV.StudyConfig(study_id=str(body.get("study_id") or f"radar-{seed}"), kind=audience, seed=seed, model_id=model_id or "unnamed",
                                constructs=("purchase_intent", "trust"), n_boot=400, control_personas_per_cell=max(4, per_cell),
                                params=E.GenerationParams(max_tokens=160),
                                budget=E.StudyBudget(max_personas=MAX_PER_CELL * MAX_SEGMENTS * 8, max_variants=12, max_calls=2500,
                                                     max_total_tokens=1_500_000, max_wall_seconds=1800.0),
                                frame_hashes={frame_name: frame.content_hash})
        result = SV.run_study(config, personas=personas, variants=variants, snapshots=[], transport=transport, embedder=embedder)
    except SV.SyntheticResearchDisabled as e:
        return {"status": "disabled", "action": "synthetic_study", "synthetic_study_run": False, "error": str(e)}
    except (ValueError, RuntimeError, OSError) as e:              # budget, transport or embedder refusals, bad configuration,
        return _refused(f"{type(e).__name__}: {e}"[:300])        # or a model/embedding endpoint that did not answer
    return summarise(result)


def summarise(result: Any) -> dict:
    """The part of a study an analyst reads first. The full study stays with the caller (``result``)."""
    from agentic_os.market.synthetic_research import contracts as C  # noqa: PLC0415
    controls = [C.to_plain(c) for c in result.controls]
    failed = [f"{c['control']} ({c['construct']})" for c in controls if not c.get("passed")]
    # Controls are run per construct, so a construct is judged by its own: a study that cannot tell an empty offer
    # from a real one ON TRUST says nothing about trust, and nothing either way about purchase intent.
    unusable = sorted({c["construct"] for c in controls if not c.get("passed")})
    ab = C.to_plain(result.abstentions)
    too_many_excluded = float(ab.get("exclusion_rate") or 0) > 0.25
    comparisons = []
    for cmp_ in (C.to_plain(c) for c in result.comparisons):
        if cmp_.get("kind") != "variants" or cmp_.get("arm") != "ssr_paper":
            continue
        rows = [{"variant_id": v["variant_id"], "mean": round(v["mean"], 3), "ci95": [round(x, 3) for x in v["ci95"]],
                 "diff_vs_first": round(v["diff_vs_first"], 3), "diff_ci95": [round(x, 3) for x in v["diff_ci95"]]} for v in cmp_["variants"]]
        withheld = cmp_["construct"] in unusable or too_many_excluded
        separated = [r["variant_id"] for r in rows[1:] if r["diff_ci95"][0] > 0 or r["diff_ci95"][1] < 0]
        comparisons.append({"cell": cmp_["cell"], "construct": cmp_["construct"], "n_paired": cmp_["n_paired"], "variants": rows,
                            "controls_passed": not withheld, "ranking": [] if withheld else cmp_["ranking"],
                            "separated_from_first": [] if withheld else separated})
    usable = [c for c in comparisons if c["controls_passed"]]
    inconclusive = not usable
    hypotheses: List[str] = []
    for c in usable:
        if c["construct"] == "purchase_intent" and c["separated_from_first"]:
            top = c["ranking"][0]
            hypotheses.append(f"In '{c['cell']}', variant {top} scored highest on stated purchase intent among synthetic respondents "
                              f"(n={c['n_paired']}); test it against the current message with real buyers before changing anything.")
    if usable and not hypotheses:
        hypotheses.append("No variant separated from the first on stated purchase intent where the controls passed; the study gives no reason to prefer one.")
    reasons = [f"control failed: {f}" for f in failed] + ([f"{ab.get('exclusion_rate'):.0%} of answers were excluded"] if too_many_excluded else [])
    if not comparisons:
        reasons.append("no cell had enough respondents to compare variants; raise per_cell")
    m = result.manifest
    return {"status": "done", "action": "synthetic_study", "synthetic_study_run": True, "synthetic": True,
            "evidence_class": "synthetic-hypothesis", "requires_human_review": True,
            "verdict": "inconclusive" if inconclusive else "hypotheses",
            "withheld_constructs": sorted(set(unusable) | ({c["construct"] for c in comparisons} if too_many_excluded else set())),
            "inconclusive_reasons": reasons,
            "hypotheses": hypotheses, "comparisons": comparisons,
            "controls": {"run": len(controls), "passed": len(controls) - len(failed), "failed": failed},
            "answers": {"calls": ab.get("elicitation_calls"), "included": ab.get("included"), "excluded_by_status": ab.get("excluded_by_status")},
            "study": {"study_id": m.study_id, "personas": m.persona_count, "seed": m.seed, "embedder": f"{m.embedder_name}@{m.embedder_revision}",
                      "models": C.to_plain(m.models), "anchor_pack_ids": dict(m.anchor_pack_ids), "truncated": m.truncated},
            "evidence_ref": result.evidence_ref(), "caveats": list(C.DISCLAIMERS), "fidelity": C.FIDELITY_STATEMENT}
