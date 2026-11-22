# Search relevance hook and explicit "unknown" (SR-04)

**Status: separate draft, opt-in, read-only, default OFF.** It adds no route, no write path, no network call, no dependency and no new service, and changes no existing behaviour unless a caller passes `relevance_fn` and/or sets `AGENTIC_OS_RELEVANCE_UNKNOWN_AWARE`.

## What it is
`SearchObservation.relevance` ("semantic query-to-page fit, 0..1") has never been populated: the Search Console mapper (`integrations/gsc.py`) copies only what the API returns and the API returns no page text, so every live observation carries the dataclass default `0.0`. As a consequence `near_win`, `missing_page` and `emergent_intent`, which require `relevance >= 0.6` (or `0.9`), are silently suppressed on live data. Two small seams fix the two halves of that:

1. **A hook that can supply the value.** `collect_search_observations(client, site, relevance_fn=...)` (default `None`: output bit-identical to before). `agentic_os/content/relevance.py` provides `make_relevance_fn(embedder, page_text_provider)`: a deterministic similarity between the query and a page representation (title, H1, meta description and a bounded body excerpt), computed with an injected `TextEmbedder` (`market/synthetic_research/embedder.py`, provisional `BAAI/bge-small-en-v1.5`). Page text comes from an injected `PageTextProvider`; the hook never fetches anything.
2. **An explicit "unknown".** `SearchObservation.relevance_source` (default `"unset"`) records where the value came from, so "never measured" is distinguishable from "measured low". Behind the flag below, detectors report "not assessable" instead of staying silent.

## What it is not
* Not a ranking predictor. No claim is made that relevance predicts rankings, CTR or conversion.
* Not a buyer-persuasion score. Relevance is a *search-matching* quantity.
* Not a crawler, an SEO auditor or a new service. No snapshotter ships in this slice; a real `PageTextProvider` (public pages only, robots honoured, `safe_url`) needs its own approval.
* Not calibrated. The 0.6 / 0.9 detector thresholds pre-date any relevance supplier (see "Calibration caveat").

## Unknown is not low
| `relevance_source` | meaning | `relevance` field |
|---|---|---|
| `unset` (default) | nobody supplied a value | `0.0` is a placeholder meaning **unknown** |
| `unavailable:<reason>` | a `relevance_fn` ran and could not assess (`no_page_text`, `empty_query`, `page_text_provider_error:<Type>`, `embedder_error:<Type>`, `degenerate_embedding`, `relevance_fn_error:<Type>`, `invalid_result`) | `0.0` placeholder, **unknown** |
| `embedding:<embedder>@<revision>` | a deterministic embedding similarity | the measured value in [0, 1] (may be low) |
| any other label | a value supplied upstream by the caller | caller-owned |

`RelevanceResult.value` is `None`, never `0.0`, when a result is unavailable. The hook never turns a failure into a number.

## Flag: `AGENTIC_OS_RELEVANCE_UNKNOWN_AWARE` (default OFF)
* **OFF**: every detector behaves exactly as before (a `0.0`/unset relevance suppresses `near_win`, `missing_page`, `emergent_intent`).
* **ON**: a detector that needs relevance and gets `relevance_source` of `unset` or `unavailable:*` returns a `WATCH` signal (same `signal_type`, confidence `0.0`) whose first evidence line is `not assessable: relevance unknown`, followed by the source and the relevance-independent facts that made the observation worth judging (impressions, position band, non-canonical page, new pairing). It is only emitted when everything *except* relevance already qualifies, so it does not create noise for observations that would not have qualified anyway. A `WATCH` is never an action: `content_intervention_queue` counts it under "watching" and every real recommendation still goes through the approval-gated queue (PROPOSE only, `CONSEQUENTIAL`). `ctr_opportunity` and `cannibalization` do not use relevance and are untouched.
* With the flag ON a relevance value must carry a source: a caller that supplies `relevance=0.8` with the default `unset` source is treated as unknown. Label caller-supplied values (for example `relevance_source="supplied:<label>"`).
* Changing the default to ON is a behaviour change and needs its own approval.

## Provenance
Every `RelevanceResult` records `embedder_name`, `embedder_revision` (the `embedder_revision` argument, else the embedder's own `revision` attribute, else a behavioural fingerprint `probe:<sha256[:12]>` of the embedding of a fixed probe string), the similarity map, `page_text_hash` and `query_text_hash` (SHA-256 of the exact sanitised texts embedded; never the text), and the text policy id. The observation keeps this in `relevance_detail`, and when a value came from the hook a `relevance provenance: ...` line is appended to the evidence of `near_win`, `missing_page` and `emergent_intent` signals. That evidence line is the only output difference for hook-supplied observations; with no hook nothing is appended.

## Method and the cosine map
Cosine similarity `c` of the query and page-representation embeddings, mapped to [0, 1] by a monotone map recorded in every result:
* `clamp01` (default): `max(0, c)`.
* `halfcosine`: `(1 + c) / 2` (as in the SSR reference code); provided for comparison only.

### Calibration caveat
With `bge-small-en-v1.5` unrelated text still has a cosine of roughly 0.35-0.6, so `halfcosine` maps *every* pair to at least 0.68 and would clear the 0.6 threshold for unrelated pairs. `clamp01` keeps unrelated pairs mostly below 0.6, but one unrelated pair in the tiny sanity set reached 0.603. The thresholds are unvalidated against any real data; see `docs/evidence/search_relevance_sanity.yaml` (Illustrative/synthetic, 12 queries x 6 pages, author-written).

## Untrusted page text
Page text is untrusted input. Before embedding it is reduced to plain text, length-bounded (title 300, H1 300, meta 500, body excerpt 2000, query 300 characters by default), and stripped of control, zero-width, bidirectional and other format characters (Unicode categories Cc, Cf, Cs, Co, Cn; separators become a space). `page_text_from_html` is a helper for providers holding raw HTML: it keeps visible text only (drops script/style/noscript/template/svg, comments and elements hidden by `hidden`, `aria-hidden="true"` or inline `display:none`/`visibility:hidden`). The text is only ever embedded: never executed, never interpreted as instructions, never put in a prompt. The hook makes no chat-model call and no network call; the only external call is `embedder.embed`, and the in-boundary rules of `HttpEmbedder` still apply.

## SSR buyer scores are never relevance
Synthetic buyer research (SSR, see `docs/SYNTHETIC_RESEARCH.md`) measures buyer resonance with synthetic respondents. It is a different construct with separate code paths and labels, and is never an input to `relevance`, rankings, detector thresholds or any measured-conversion field. Enforced by: `relevance.py` imports nothing from the SSR scoring/study modules (static test); `RelevanceResult` refuses a source starting with `ssr` or `synthetic` and refuses any value whose source is not an embedding; `apply_relevance` accepts only `embedding:` sources.

## Evidence bridge (synthetic studies stay hypotheses)
`StudyEvidenceRef` (`market/synthetic_research/evidence_ref.py`) is a frozen, serialisable pointer to a synthetic study (study id, manifest hash, construct, embedder name, `synthetic=True`, label `HYPOTHESIS, synthetic, unvalidated`). It serialises to a `synthetic-study:v1:` string so it fits the existing `Experiment.evidence_refs` / `Opportunity.evidence_refs` tuples with no contract change: `StudyEvidenceWriter` plugs into `plan_experiments(writer=...)` and labels the proposed change; `attach_study_refs` appends the refs to a planned `Experiment` (attach after planning so `explain`'s count of supporting observations is not inflated). It is not a metric: `outcomes_join.METRIC_SOURCES` gains no synthetic source, and `OutcomeStore` keeps refusing simulated events.

## Validation plan (when read-only Search Console access is authorised)
Not done, and not claimed. With an approved read-only property and an approved page-text snapshotter: (1) compute relevance for the query/page pairs; (2) compare it descriptively with observed CTR and position stability over a held-out window (distribution, rank correlation, no causal language); (3) choose the map and thresholds from those data, not from this document; (4) only then consider changing the flag default. No relevance value should drive a recommendation before step 3.

## Using it
```python
from agentic_os.content.relevance import make_relevance_fn
from agentic_os.integrations.gsc import collect_search_observations

relevance_fn = make_relevance_fn(embedder, page_text_provider)       # both injected
observations = collect_search_observations(client, site_url, relevance_fn=relevance_fn)
```
