# Synthetic Buyer Research (SSR) — scoring core

**Status: opt-in, default OFF (`AGENTIC_OS_SYNTHETIC_RESEARCH`), read-only, synthetic-only.** Hypothesis generation and early concept screening for website messaging and search-intent fit. It is **not** customer research, not a conversion or ranking predictor, and its output is never first-party evidence (it must never be written to an `OutcomeStore` or the first-party ledger).

This first slice (`agentic_os/market/synthetic_research/`) is the deterministic scoring core only: `ssr.py` (pure Python, no I/O), `anchors.py` (local anchor packs), `embedder.py` (`TextEmbedder` protocol, a local sentence-transformers embedder and an in-boundary-only HTTP embedder), `pipeline.py` (glue). It adds no route, no write path, no dependency and no change to existing behaviour.

## Method and fidelity (read before quoting any number)
Semantic Similarity Rating follows Maier et al., *LLMs Reproduce Human Purchase Intent via Semantic Similarity Elicitation of Likert Ratings*, arXiv:2510.08338 (CC BY 4.0), App. A.4.3: embed a free-text answer and five anchor statements, take similarities, subtract the minimum within each anchor set, normalise to a 5-bin pmf (equation 8), optionally smooth with a temperature (equation 9), and average over several anchor sets.

* **The paper's six anchor sets are not published** (Appendix C.1 describes them without listing the statements; the authors' reference package, Apache-2.0, ships only two toy sets). `anchors.py` therefore contains **local, independently authored packs** with a version and content hash per construct (purchase intent, trial/pilot intent, relevance, trust). **Parity with the paper's reported figures cannot be tested** and none is claimed.
* Two similarity variants are implemented and always reported: `paper-cosine` (equation 7) and `ref-halfcosine` ((1+cos)/2 as in the reference code). With `epsilon = 0` they give identical pmfs (the affine map cancels); they differ only for `epsilon > 0`.
* Degenerate inputs (all similarities equal) return a uniform pmf **and are flagged**; blank responses are refused; nothing is silently repaired.
* The paper reports its results for the paper's own embedding model and LLMs on personal-care concepts. Transfer to SME services, B2B software and enterprise buying committees is **unvalidated** and must be established with held-out human data.

## Embeddings
agentic_os has no embedding client. `TextEmbedder` is injected. A chat model's `/v1/embeddings` was measured to be non-discriminative for SSR (cosines 0.93–0.95 against all five anchors for both a positive and a negative response); use a dedicated embedding model. `HttpEmbedder` refuses any host that is not inside the trust boundary (`app.llm._is_in_boundary_host`); the paper's external, paid embedding API is not a default.

## Sanity check (Illustrative/synthetic; internally reproduced, author-labelled data, n = 20)
Hand-written synthetic answers with an intended polarity 1–5 (see `docs/evidence/synthetic_research_anchor_sanity.md`), scored with the local purchase-intent pack at the paper's defaults (`epsilon = 0`, `T = 1`): Spearman 0.97 with `BAAI/bge-small-en-v1.5` (group means 2.14 / 2.47 / 3.10 / 3.82 / 4.09) and 0.90 with `all-MiniLM-L6-v2` (2.72 … 3.38). Rank order is recovered but the scale is compressed towards the middle. This checks plumbing and anchor ordering only; it is **not** validation against human purchase intent.

## Second slice: personas, elicitation, study runner, controls (still opt-in, read-only, synthetic-only)
What is **published** (implemented, eq. 7-9, minimum subtraction, normalisation, mean over anchor sets), what is **independently constructed** (the local anchor packs, plus a second independently worded `purchase_intent`/`trust` pack in `anchors_alt.py`) and what is **new here** (everything below) are kept apart on purpose. Nothing in this slice claims or implies parity with the paper's reported figures.

| module | what it does |
|---|---|
| `contracts.py` | frozen dataclasses, `synthetic` can only be `True`; `PersonaSpec`, `ConceptVariant` (label never shown to a model; prices are hypotheses), `SourceSnapshot`, `SyntheticResponse`, `SSRDistribution`, `StudyManifest` (embedder name **and revision**, pack ids, seeds, hashes, costs, `untrusted_content_flags`, held-out split, sealed planted order), `StudyResult.evidence_ref()` (a plain pointer, not a metric), `Recommendation` (always `requires_human_review`, gate `pending`). |
| `personas.py`, `scenarios/*.yaml` | seeded, stratified sampler over a YAML frame; six SME niches; enterprise committee of seven roles x scenarios E1-E6. All weights are labelled hypothetical. |
| `elicitation.py` | blind neutral prompts, one per construct; no tools in the request type or payload; budget refused before any call; per-call seeds; `LocalOpenAITransport` refuses hosts outside the trust boundary and redirects; `governed_route_receipt` resolves the endpoint through `GovernedLLM`. |
| `sanitize.py` | public page text is untrusted data: HTML reduced to visible text, injection patterns detected, neutralised and counted, delimited block; answers containing URLs, tool-call syntax or operator instructions are quarantined. Heuristic, not a guarantee. |
| `study.py` | persona x variant x construct, SSR (paper-cosine, half-cosine, epsilon/T sweep), DLR and FLR comparison arms, paired bootstrap over personas, controls, themes citing response and page sentence, unsupported-claim detector, abstention accounting. |
| `controls.py`, `planted.py`, `anchor_sensitivity.py` | positive/negative controls; planted-order benchmark (order fixed by construction before scoring and sealed); anchor ablations and negative controls. |
| `committee.py` | structural buying-committee model (veto, champion, budget, feasibility) with sensitivity; outputs a category, never a probability. |
| `actionability.py` | blinded rubric for recommendations (deterministic checker + judge prompts + agreement). LLM-judged actionability is weak evidence. |
| `stimuli.py`, `report.py` | held-out split and neutral-labelled stimulus pack for a blinded human pilot; JSON/Markdown/JSONL export that refuses any first-party sink. |

Safety properties (each has a test): the flag is OFF by default and nothing runs when it is off; no write path to a website, queue, publisher, `OutcomeStore` or ledger (an AST test forbids the imports and `export_report` refuses non-directory sinks); the only network client is `LocalOpenAITransport` (plus the existing `HttpEmbedder`), both in-boundary only; the model receives no tools, files, browsing or network; page text can neither change a manifest, a budget nor a score; every recommendation is a HYPOTHESIS for mandatory human review.

Studies compare only like with like: `compare_results` refuses to compare studies with different embedders or revisions unless the caller passes `allow_embedder_mismatch=True`, and then the output carries a visible `embedder_mismatch` flag.

### Local Qwen servers: what was measured (one machine, 2026-11-22; re-verify on any other server)
* `chat_template_kwargs: {"enable_thinking": false}` is accepted by both loopback OpenAI-compatible servers and is needed: with thinking enabled a 120-160 token budget was spent on reasoning and the visible answer was empty (6/6 and 6/6). `strip_reasoning` also removes `<think>` blocks defensively.
* The per-call `seed` parameter is honoured: 6/6 identical texts across three repeats on both a 27B 8-bit model and a Flash-class model, versus 0/6 without a seed. At `temperature = 0` the Flash-class server was **not** reproducible (0/6) while the 27B one was (6/6), so the module controls determinism through per-call seeds, not through temperature 0. An earlier single-prompt probe on the Flash server showed a divergence after a common prefix, so treat seed reproducibility as "mostly", not as a guarantee, and record prompt hashes.
* The servers' `/v1/embeddings` use the chat model and are not usable for SSR; embeddings come from an injected `TextEmbedder` and the model revision is recorded in the manifest.

## Market Radar capability: `radar.synthetic_study`
`apps/market-radar/synthetic.py` exposes one small study as a read-only Market Radar capability. Input: 2 to 4 message variants (`{id, headline, body, cta}`), an `audience` (`sme`: one to three niches from the SME frame; `enterprise`: every committee role in one scenario), `per_cell` (1 to 8), `seed`. Output: per construct, each variant's mean with a 95% interval and its difference from the first variant; the controls that ran and which failed; how many answers were excluded; the manifest reference.

* Off unless `AGENTIC_OS_SYNTHETIC_RESEARCH` is set; with it off the capability answers `disabled` and touches nothing.
* Respondent model: `SYNTHETIC_RESEARCH_LLM_URL` and `SYNTHETIC_RESEARCH_LLM_MODEL` (in-boundary only). Embedder: `SYNTHETIC_RESEARCH_EMBEDDER=sentence-transformers:<model>` (in-process) or `SYNTHETIC_RESEARCH_EMBED_URL` and `SYNTHETIC_RESEARCH_EMBED_MODEL` (in-boundary only); `SYNTHETIC_RESEARCH_EMBED_REVISION` records the pinned revision.
* Every result carries `synthetic: true`, `evidence_class: synthetic-hypothesis` and `requires_human_review: true`.
* Controls are judged per construct: a construct whose controls fail has its ranking withheld (`withheld_constructs`). When no construct passes, or more than a quarter of the answers are excluded, the study is `verdict: inconclusive` with no ranking and no hypotheses.
* The module imports nothing from the watch service, an outcome store or the ledger, and writes nowhere.
