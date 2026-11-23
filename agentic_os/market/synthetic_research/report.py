"""Reproducible export: JSON + Markdown report and a raw-responses JSONL, written to a directory path only.

The only sink is a filesystem directory given as ``str``/``Path``. Any other sink (an outcome store, a ledger, a
queue, a publisher, any object with ``append``/``write``/``publish``) is REFUSED with :class:`SyntheticViolation`,
and nothing in this package imports those types. Reports carry the standing disclaimers, per-segment sample sizes
and uncertainty, and never present a synthetic preference share as customer demand: the only per-variant
"probabilities" shown are bootstrap stability shares of THIS panel and are labelled as such.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import DISCLAIMERS, FIDELITY_STATEMENT, Recommendation, StudyResult, SyntheticViolation, to_plain

_FORBIDDEN_NAMES = ("outcomestore", "firstpartyledger", "ledger", "queue", "publisher")
HYPOTHESIS_BANNER = ("**Every finding below is a HYPOTHESIS (synthetic, unvalidated).** Human review is mandatory before any "
                     "experiment, website change or publishing; nothing automated may act on this report.")


def _check_sink(target: Any) -> Path:
    if not isinstance(target, (str, os.PathLike)):
        raise SyntheticViolation(f"synthetic reports can only be exported to a filesystem directory, not to {type(target).__name__}")
    p = Path(target)
    if any(w in part.lower() for part in p.parts for w in ("outcomestore", "first_party_ledger", "firstpartyledger")):
        raise SyntheticViolation("refusing a path that looks like a first-party store")
    return p


def _f(x: Any, nd: int = 2) -> str:
    return "n/a" if x is None or x != x else f"{x:.{nd}f}"


def render_markdown(result: StudyResult, *, title: str = "", extra: Sequence[Tuple[str, str]] = (),
                    recommendations: Sequence[Recommendation] = (), max_quotes: int = 2) -> str:
    m = result.manifest
    L: List[str] = [f"# {title or 'Synthetic buyer research report: ' + m.study_id}", "",
                    f"> {HYPOTHESIS_BANNER}", "", "## Standing disclaimers", ""]
    L += [f"- {d}" for d in m.disclaimers]
    L += ["", "## Fidelity statement", "", m.fidelity_statement, "",
          "Implemented from the paper: eq. 7-9 (similarity, min-subtraction, normalisation, temperature) and the mean over "
          "anchor sets. Independently constructed: the local anchor packs. Everything else (personas, prompts, controls, "
          "comparison arms, themes, committee model) is new here. No parity with the paper's reported figures is claimed.", "",
          "## Provenance", "",
          f"- study: `{m.study_id}` ({m.kind}), created {m.created_at}, seed {m.seed}, prompt template `{m.prompt_template_version}`",
          f"- models: {json.dumps(to_plain(m.models))}",
          f"- generation: {json.dumps(to_plain(m.generation_params))}",
          f"- embedder: `{m.embedder_name}` revision `{m.embedder_revision}`",
          f"- anchor packs: {json.dumps(to_plain(m.anchor_pack_ids))}",
          f"- SSR arms: " + ", ".join(f"{a['arm']} ({a['similarity']}, eps={a['epsilon']}, T={a['temperature']})" for a in m.ssr_arms),
          f"- personas: {m.persona_count}; variants: {json.dumps(to_plain(m.variant_hashes))}",
          f"- snapshots: " + "; ".join(f"{s['url']} sha256={s['sha256'][:16]} fetched {s['fetched_at']}" for s in m.snapshot_refs),
          f"- counts: {json.dumps(to_plain(m.counts))}; cost: {json.dumps(to_plain(m.cost))}; truncated: {m.truncated}",
          f"- untrusted-content flags: {json.dumps(to_plain(m.untrusted_content_flags))}", ""]
    if m.planted_order:
        po = m.planted_order
        L += ["## Planted order (sealed before scoring)", "",
              f"Best to worst by construction: {' > '.join(po.get('ids_best_to_worst', []))}; sealed hash `{po.get('sealed_hash', '')[:16]}`. {po.get('rule', '')}", ""]
    if m.concept_split:
        L += ["## Tuning / held-out split (drawn before scoring)", "",
              f"seed {m.concept_split.get('seed')}, hash `{str(m.concept_split.get('split_hash', ''))[:16]}`; tuning: {', '.join(m.concept_split.get('tuning', []))}; "
              f"held-out: {', '.join(m.concept_split.get('held_out', []))}. Settings may be chosen on the tuning half only.", ""]
    L += ["## Limitations", ""] + [f"- {x}" for x in m.limitations] + [""]
    a = result.abstentions
    L += ["## Abstentions and exclusions", "",
          f"Elicitation calls {a.get('elicitation_calls')}, included {a.get('included')}, excluded {json.dumps(a.get('excluded_by_status'))} "
          f"(rate {_f(a.get('exclusion_rate'), 3)}); degenerate SSR responses {a.get('degenerate_ssr_responses')}; unparsed DLR {a.get('dlr_unparsed')}, FLR {a.get('flr_unparsed')}.", ""]
    L += ["## Variant comparison per segment (SSR paper-cosine mean on the 1-5 synthetic scale)", "",
          "`p_top` = share of bootstrap resamples of THIS synthetic panel in which the variant has the highest mean: a stability "
          "measure of the panel, not a preference share and not demand. Intervals are 95% bootstrap intervals over synthetic personas.", ""]
    for c in result.comparisons:
        if c["kind"] != "variants":
            continue
        L += [f"### {c['cell']} / {c['construct']} (n paired personas = {c['n_paired']})", "",
              "| variant | mean | 95% CI | diff vs first | diff 95% CI | p_top |", "|---|---|---|---|---|---|"]
        for v in c["variants"]:
            L.append(f"| {v['variant_id']} {v['label']} | {_f(v['mean'])} | {_f(v['ci95'][0])} to {_f(v['ci95'][1])} | "
                     f"{_f(v['diff_vs_first'])} | {_f(v['diff_ci95'][0])} to {_f(v['diff_ci95'][1])} | {_f(v['p_top'])} |")
        L.append(f"\nRanking (hypothesis): {' > '.join(c['ranking'])}\n")
    L += ["## Per-cell sample sizes", "", "| cell | construct | variant | n included | excluded | mean | sd |", "|---|---|---|---|---|---|---|"]
    for r in result.cells:
        L.append(f"| {r['cell']} | {r['construct']} | {r['variant_id']} | {r['n_included']}/{r['n_personas']} | {json.dumps(r['excluded'])} | "
                 f"{_f(r.get('mean_rating'))} | {_f(r.get('sd'))} |")
    arms = [c for c in result.comparisons if c["kind"] == "arm_summary"]
    if arms:
        L += ["", "## Method comparison (direct rating vs text-then-rating vs SSR)", "",
              "| construct | arm | n | mean | sd | mean abs dev from 3 | share within 0.5 of 3 | mean between-variant spread |", "|---|---|---|---|---|---|---|---|"]
        for c in arms:
            L.append(f"| {c['construct']} | {c['arm']} | {c['n']} | {_f(c['mean'])} | {_f(c['sd'])} | {_f(c['mean_abs_dev_from_3'])} | "
                     f"{_f(c['share_within_0.5_of_3'])} | {_f(c['mean_between_variant_spread'])} |")
    L += ["", "## Controls", "", "| control | construct | expectation | observed | passed |", "|---|---|---|---|---|"]
    for c in result.controls:
        L.append(f"| {c['control']} | {c['construct']} | {c['expectation']} | {json.dumps(to_plain(c.get('observed')))} | {c.get('passed')} |")
    L += ["", "## Objection themes (deterministic keyword method; each cites the response and the page sentence)", ""]
    for t in sorted(result.themes, key=lambda t: (t["variant_id"], t["construct"], -t["n_responses"]))[:60]:
        q = t["evidence"][0]
        L.append(f"- **{t['variant_id']} / {t['construct']} / {t['theme']}**: {t['n_responses']} of {t['n_included']} responses "
                 f"(gap share {_f(t['gap_share'])}). Page sentence `{t['cited_concept_sentence_id']}`: \"{t['cited_concept_sentence'][:160]}\". "
                 f"Evidence `{q['response_id']}`: \"{q['quote'][:200]}\"")
    L += ["", f"## Unsupported-claim flags: {len(result.groundedness)} responses flagged (heuristic; see false-positive note in docs)", ""]
    for g in list(result.groundedness)[:15]:
        L.append(f"- `{g['response_id']}`: " + "; ".join(f"{f['kind']}: {f['text']}" for f in g["flags"][:3]))
    for title2, body in extra:
        L += ["", f"## {title2}", "", body]
    if recommendations:
        L += ["", "## Recommendations (HYPOTHESES for human review)", ""]
        for r in recommendations:
            L += [f"### {r.rec_id}: {r.scope}", "", r.text, "", f"- label: {r.label}; requires_human_review: {str(r.requires_human_review).lower()}; "
                  f"review gate: {r.review_gate_status}", "- page text cited: " + " | ".join(f"\"{x}\"" for x in r.page_text_cited),
                  "- synthetic evidence: " + ", ".join(r.synthetic_evidence), f"- validation: {r.validation_plan}", ""]
    return "\n".join(L) + "\n"


def export_report(result: StudyResult, target: Any, basename: str = "study", *, title: str = "",
                  extra: Sequence[Tuple[str, str]] = (), recommendations: Sequence[Recommendation] = ()) -> Dict[str, str]:
    """Write ``<basename>.json``, ``<basename>.md`` and ``<basename>.responses.jsonl`` into directory ``target``."""
    out = _check_sink(target)
    out.mkdir(parents=True, exist_ok=True)
    name = Path(basename).name                                   # never a path component
    paths = {"json": out / f"{name}.json", "markdown": out / f"{name}.md", "responses": out / f"{name}.responses.jsonl"}
    doc = result.to_dict()
    doc["recommendations"] = [to_plain(r) for r in recommendations]
    doc["evidence_ref"] = result.evidence_ref()
    paths["json"].write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    paths["markdown"].write_text(render_markdown(result, title=title, extra=extra, recommendations=recommendations), encoding="utf-8")
    with open(paths["responses"], "w", encoding="utf-8") as fh:
        for r in result.responses:
            fh.write(json.dumps(to_plain(r), ensure_ascii=False, sort_keys=True) + "\n")
    return {k: str(v) for k, v in paths.items()}


__all__ = ["HYPOTHESIS_BANNER", "render_markdown", "export_report"]
