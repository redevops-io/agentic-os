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
