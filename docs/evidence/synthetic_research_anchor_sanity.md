# Evidence card: SSR anchor sanity check (synthetic, internally reproduced)

Label: **Illustrative/synthetic — Internally reproduced.** Not validation against human data.

* Data: 20 hand-written synthetic respondent answers with an author-assigned polarity 1–5 (`synthetic_research_anchor_sanity_corpus.json`).
* Anchors: `PURCHASE_INTENT` pack `local-v1` (six sets, `AnchorPack.pack_id` records the content hash).
* Embedders (CPU, sentence-transformers): `BAAI/bge-small-en-v1.5`, `sentence-transformers/all-MiniLM-L6-v2`.
* Scoring: `ssr_score`, variant `paper-cosine`.

| Embedder | epsilon | T | Spearman(label, mean) | group means by label 1..5 | mean entropy |
|---|---|---|---|---|---|
| bge-small-en-v1.5 | 0 | 1.0 | 0.969 | 2.14 2.47 3.10 3.82 4.09 | 1.36 |
| bge-small-en-v1.5 | 0.05 | 1.0 | 0.914 | 2.48 2.72 2.95 3.52 3.79 | 1.50 |
| bge-small-en-v1.5 | 0 | 0.5 | 0.975 | 1.93 2.39 3.13 3.98 4.33 | 1.21 |
| bge-small-en-v1.5 | 0 | 0.25 | 0.981 | 1.74 2.31 3.18 4.09 4.47 | 1.02 |
| all-MiniLM-L6-v2 | 0 | 1.0 | 0.901 | 2.72 3.00 3.15 3.37 3.38 | 1.54 |
| all-MiniLM-L6-v2 | 0 | 0.25 | 0.779 | 2.53 3.01 3.18 3.55 3.65 | 1.39 |

Limits: n = 20, author-written text and labels (circular), anchors written by the same project, one construct. A chat model's own `/v1/embeddings` output was not usable (cosines 0.93–0.95 for every anchor). Do not quote these numbers as predictive validity.
