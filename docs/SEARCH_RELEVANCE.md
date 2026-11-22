# Search relevance hook and explicit "unknown" (SR-04)

**Status: separate draft, opt-in, read-only, default OFF.** It adds no route, no write path, no network call, no dependency and no new service. With `AGENTIC_OS_RELEVANCE_UNKNOWN_AWARE` unset and no `relevance_fn`, every existing input produces the output it produced before this change (proof below), apart from the short list of deliberate non-identities.

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
The relevance **source is authoritative**; the numeric `relevance` field is only a value when the source says so.

| `relevance_source` | meaning | state |
|---|---|---|
| `unset` (default) with `relevance == 0.0` | nobody supplied a value; `0.0` is the legacy placeholder | **unknown** |
| `unset` with a non-zero value | a value supplied upstream by an existing integration, unlabelled | measured: judged on its number exactly as before; with the flag on its evidence says "supplied, unlabelled" |
| `unavailable:<reason>` | a `relevance_fn` ran and could not assess (`no_page_text`, `empty_query`, `page_text_provider_error:<Type>`, `embedder_error:<Type>`, `degenerate_embedding`, `degenerate_embedder`, `relevance_fn_error:<Type>`, `invalid_result`) | **unknown**, whatever number sits in `relevance` (a placeholder) |
| `embedding:<embedder>@<revision>` | a deterministic embedding similarity | measured, in [0, 1], may be low; a measured `0.0` is **low**, never unknown |
| any other label (recommended: `supplied:<label>`) | a value supplied upstream and labelled by the caller | measured |
| any source with NaN, infinity or a value outside [0, 1] | invalid | **invalid**: treated as unknown by the unknown-aware detectors; legacy numeric behaviour with the flag off is untouched |

Source prefixes are matched case-insensitively after trimming (`" UNSET"`, `"Unavailable:x"` read as unknown, never as measured low). `RelevanceResult.value` is `None`, never `0.0`, when a result is unavailable. The hook never turns a failure into a number. `relevance_state(o)` returns `unknown` / `invalid` / `low` / `ok`.

## Flag: `AGENTIC_OS_RELEVANCE_UNKNOWN_AWARE` (default OFF)
* **OFF**: every legacy observation is judged on its number exactly as before (relevance `0.0` suppresses `near_win`, `missing_page`, `emergent_intent`). The one case that differs by design is a hook-written `unavailable:*` source: its placeholder number never drives a detector (it would otherwise produce a signal that says both "relevance 0.90" and "unknown").
* **ON**: a detector that needs relevance and gets an *unknown* or *invalid* relevance returns a `WATCH` signal (same `signal_type`, confidence `0.0`) whose first evidence line is `not assessable: relevance unknown`, followed by the source and the relevance-independent facts that made the observation worth judging (impressions, position band, non-canonical page, new pairing). It is only emitted when everything *except* relevance already qualifies, so it does not create noise. A `WATCH` is never an action: `content_intervention_queue` counts it under "watching" and every real recommendation still goes through the approval-gated queue (PROPOSE only, `CONSEQUENTIAL`). `ctr_opportunity` and `cannibalization` do not use relevance and decide exactly as before.
* **Existing integrations that supply a value with the default source** (`relevance=0.8`, source `unset`) are *not* demoted with the flag on: a non-zero unlabelled value is a supplied, measured value and behaves as today (the evidence notes it is unlabelled). Only the legacy default `0.0` with source `unset` is unknown. Recommendation: label supplied values (`relevance_source="supplied:<label>"`); an upstream value that is deliberately `0.0` must be labelled too, or it reads as unknown.
* Changing the default to ON is a behaviour change and needs its own approval.

## Where unknown is distinguished (pipeline and consumers)
| Stage | Treatment of unknown / measured low |
|---|---|
| `SearchObservation` construction / `to_observation` | default source `unset`; a hook-written `0.0` carries `embedding:*`, so the two observations are never equal |
| `collect_search_observations(..., relevance_fn)` | `None` fn: untouched. A failure, `None`, NaN, out-of-range, non-numeric or non-embedding source, or a result object whose attributes raise, becomes `unavailable:<reason>` (with the embedder provenance when the hook supplied it), never a value. Reserved prefixes are canonicalised to lower case |
| `relevance_state(o)` | `unknown` / `invalid` / `low` / `ok`; explains why a relevance-gated detector stayed silent |
| `near_win`, `missing_page`, `emergent_intent` | flag off: unknown and low are both silent (as before). Flag on: unknown/invalid gives WATCH "not assessable: relevance unknown"; low gives no signal; ok gives the normal signal |
| `ctr_opportunity`, `cannibalization` | decisions unchanged; evidence states the relevance source (hook-written sources always; every source when the flag is on); cannibalization lists each competing page's reading and notes a measured/unknown mix |
| `search_signals_from_observations` | differing readings of the same (site, page, query) (two measured values, or measured next to unknown) yield a WATCH `RELEVANCE_CONFLICT` signal listing every reading; a `near_win` PROPOSE that depends on a conflicted labelled reading is held at WATCH. Legacy `unset` rows never take part. Nothing is averaged or overwritten. Active when the flag is on, or for groups containing a hook reading |
| `content_intervention_queue` | only PROPOSE signals become candidates, so not-assessable and conflict WATCH signals are never actions; counted in `watching` and broken out as `not_assessable` / `relevance_conflicts` (keys present only when non-zero) |
| `content/portfolio.py` `scan_portfolio` | rollup gains `total_not_assessable` / `total_relevance_conflicts`, only when non-zero (otherwise unchanged) |
| `growth/goals.py` (`measure_*`) | read query, page, position, impressions and CTR only: goal progress is identical whatever the relevance source (tested). `RELEVANCE_CONFLICT` advances no goal |
| `growth/loop.py`, `content/portfolio.py` `scan_site` | call `collect_search_observations` without `relevance_fn`: observations stay `unset`; flag off is identical; flag on reports "not assessable" for the default `0.0` |
| `sidekick/catalog.py` | a code reference string only |
| Projects UI tab | fetched at build time; not verifiable here |

### Conflicts, precisely
The key is `(site_id, page_url, the exact sanitised query text)`: different queries (including every non-Latin query) never conflict, and one tenant's reading is never attributed to another. Readings that differ by less than 1e-9 are the same reading. Equal values from *different* embedders or revisions are not a conflict (the claim agrees; the provenance differs, and both readings are listed if another reading conflicts). The signal does not depend on input order.

### Behaviour matrix (outcome per cell; asserted in `tests/test_search_relevance_matrix.py` and `tests/test_search_relevance_round2.py`)
| Case | Flag OFF | Flag ON |
|---|---|---|
| Embedder raises / wrong shape / NaN / zero vectors | source `unavailable:embedder_error:*` or `unavailable:degenerate_embedding`; gated detectors silent | WATCH not assessable naming that source and the embedder; no action |
| Constant embedder (same vector for different texts) | `unavailable:degenerate_embedder`; silent | WATCH not assessable |
| Page text None / empty / only invisible characters | `unavailable:no_page_text`; silent | WATCH not assessable |
| Oversized or hostile page text | bounded and sanitised, measured value; the injected instruction does not raise the score | same |
| Same pair read as 0.8, unknown, 0.1 | one `RELEVANCE_CONFLICT` listing all three; the 0.8 near-win held at WATCH; 0.1 no signal; unknown silent | same, plus WATCH not assessable for the unknown reading |
| Same query, two embedders, different values | `RELEVANCE_CONFLICT` listing both embedders and values | same |
| Identical readings, equal values from two embedders, only unknown readings, different queries or sites | no conflict | no conflict |
| Cannibalization with an unknown member | still PROPOSE; per-page sources and the measured/unknown mix in the evidence | same |
| Legacy input (no hook) | bit-identical to base (see below) | decisions unchanged, except relevance-gated detectors report not assessable for the default `0.0`; evidence gains a `relevance_source=` line |
| Unset / unavailable (even with a number) / measured 0.05 / measured 0.8 | silent / silent / silent / PROPOSE | WATCH not assessable / WATCH not assessable (source differs) / no signal, state `low` / PROPOSE with provenance |
| Hook-computed `0.0` vs the legacy default `0.0` | different observations: `embedding:*` (state `low`) vs `unset` (state `unknown`) | `near_win`: no signal vs WATCH not assessable |

## Bit-identity evidence (flag off, no hook)
* `tests/golden/search_relevance_bitid_base.json` holds 159 digests produced by running `tests/search_relevance_bitid_support.py` against the unmodified base commit (d9210fb). It covers 2,813 observations (boundary grids, NaN/inf, non-Latin and control-character queries, cannibalization families) through every detector, `search_signals_from_observations`, `cannibalization`, the mapper and `collect_search_observations`, `content_intervention_queue` and the growth goal measures, for each of: dataclass observations, `SimpleNamespace` and `namedtuple` duck types, and list, tuple, single-use generator and `iter(tuple)` inputs. `tests/test_search_relevance_bitid.py` recomputes all of it with the flag unset and with 18 other values (`""`, `0`, `false`, `off`, `garbage`, ...): every digest is identical.
* An independent reviewer's harness (about 2,800 observations, 17 flag values) gives identical output for every call group on the final code.
* The quirk that a single-use iterator hides `CANNIBALIZATION` (the input is consumed before that detector runs) is base behaviour and is preserved.

### Deliberate non-identities
1. `SearchObservation` gains two defaulted fields at the end (`relevance_source`, `relevance_detail`), so `repr`, `dataclasses.asdict` and `fields()` show them; positional construction of the original nine fields is unchanged.
2. `SignalType` gains a last member `RELEVANCE_CONFLICT`.
3. New public names in `content/search_signals.py` (`relevance_state`, `relevance_reading`, `relevance_conflicts`, `relevance_unknown`, `hook_originated`, `conflict_key`, `is_not_assessable`, `relevance_unknown_aware`, `NOT_ASSESSABLE`, `RELEVANCE_UNKNOWN_AWARE_ENV`) and `integrations/gsc.py` (`apply_relevance` and its imports).
4. Observations written by the hook (`embedding:*` / `unavailable:*`) add one relevance-source evidence line to every signal they produce, and an `unavailable:*` placeholder number never drives a detector. Both are reachable only by passing `relevance_fn`.
5. With the flag on (opt-in), signals gain a `relevance_source=` evidence line and the legacy default `0.0` is reported as not assessable.
6. `content_intervention_queue` and `scan_portfolio` gain optional keys that appear only when non-zero.
7. Market evidence guards (outside `content/` and `market/synthetic_research/`): `FirstPartyLedger.record` refuses non-first-party sources; `plan_experiments`' explain text does not count `synthetic-study:` refs; `freeze_experiment` refuses a hypothesis carrying one. Real sources and refs are unaffected.

## Provenance
Every `RelevanceResult` records `embedder_name`, `embedder_revision`, the similarity map, `page_text_hash` and `query_text_hash` (SHA-256 of the exact sanitised texts embedded; never the text) and the text policy id (`visible-text-v2`). The embedder revision is the `embedder_revision` argument, else the embedder's own `revision` attribute, else a behavioural fingerprint `probe:<sha256[:12]>` of the embedding of a fixed probe string. If the probe fails the result says `revision=unrecorded` with `provenance_incomplete=true` and the probe is retried on the next call (a failure is never cached). The same probe embeds a second, unrelated text: an embedder that returns the same vector for both is reported as `unavailable:degenerate_embedder`. Embedder name, revision and sources are sanitised (no control, zero-width or bidi characters, 120-character cap).

The observation keeps this in `relevance_detail`. Hook-written sources add a `relevance provenance: ...` (embedding) or `relevance unknown: ...` (unavailable) evidence line to every signal that observation produces, including `ctr_opportunity` and `cannibalization`; with the flag on every signal states its relevance source.

## Method and the cosine map
Cosine similarity `c` of the query and page-representation embeddings, mapped to [0, 1] by a monotone map recorded in every result:
* `clamp01` (default): `max(0, c)`.
* `halfcosine`: `(1 + c) / 2` (as in the SSR reference code); provided for comparison only.

The page representation is title, H1, meta description and a bounded body excerpt, each sanitised and passed through `collapse_repeats` (drops every token that completes a word 3-gram already seen in that part, so a verbatim repeated sentence, list or run counts once). The relevance function memoises page text per `page_url` and embeddings per text hash under a lock (thread-safe, one provider call per page); build a new function for each collection run so page text is never stale.

### Calibration caveat
With `bge-small-en-v1.5` unrelated text still has a cosine of roughly 0.35-0.6, so `halfcosine` maps *every* pair to at least 0.68 and would clear the 0.6 threshold for unrelated pairs. `clamp01` keeps unrelated pairs mostly below 0.6, but one unrelated pair in the tiny sanity set reached 0.603. The thresholds are unvalidated against any real data; see `docs/evidence/search_relevance_sanity.yaml` (Illustrative/synthetic, 12 queries x 6 pages, author-written).

### Residual: keyword stuffing
A page that literally contains the query text scores as relevant to it; that is what a semantic-similarity measure does. Measured with `bge-small-en-v1.5` and `clamp01` on the author-written set (illustrative, n = 12 queries): an honest page scored a mean 0.517 (2 of 12 queries at or above 0.6); the same page with every query pasted into its body scored 0.624 (8 of 12) before and 0.613 (8 of 12) after `collapse_repeats`, and 0.608 (7 of 12) with each query pasted once. The repeat-collapse mitigation therefore only removes repetition; it does not stop a page that contains each query once. The exposure is bounded because `near_win` also requires impressions and a position in the 5-20 band: an attacker needs an existing ranking, and a relevance value never creates a PROPOSE on its own (every recommendation is still approval-gated). Do not treat a high relevance as evidence that a page is honest.

## Untrusted page text
Page text is untrusted input. Before embedding it is reduced to plain text, length-bounded (title 300, H1 300, meta 500, body excerpt 2000, query 300 characters by default), and stripped of control, zero-width, bidirectional and other format characters (Unicode categories Cc, Cf, Cs, Co, Cn; separators become a space). `page_text_from_html` is a helper for providers holding raw HTML: it keeps visible text only (drops script/style/noscript/template/svg, comments and elements hidden by `hidden`, `aria-hidden="true"` or inline `display:none`/`visibility:hidden`). It is linear-time: a hidden-depth counter and a per-tag open count replace stack scans; input is capped at 200,000 characters, nesting at 256, start tags at 20,000 and collected text at 100,000 characters, beyond which the document is truncated. Hostile nesting finishes in well under a second (tested) and the helper never raises. The text is only ever embedded: never executed, never interpreted as instructions, never put in a prompt. The hook makes no chat-model call and no network call; the only external call is `embedder.embed`, and the in-boundary rules of `HttpEmbedder` still apply.

## SSR buyer scores are never relevance
Synthetic buyer research (SSR, see `docs/SYNTHETIC_RESEARCH.md`) measures buyer resonance with synthetic respondents. It is a different construct with separate code paths and labels, and is never an input to `relevance`, rankings, detector thresholds or any measured-conversion field.

The guarantee is **architectural**: `relevance.py` has no import path to the SSR scoring/study modules (a static test enforces it) and its only numeric operation is the cosine of two vectors returned by the injected embedder. **The embedder is trusted code**: a hostile embedder could encode any number as an angle between its output vectors, and no check here could detect that. The source-prefix checks (`RelevanceResult` refuses sources and embedder names with an `ssr`/`synthetic` token and any value whose source is not `embedding:`; `apply_relevance` accepts only `embedding:` sources) catch accidents and mislabelling only; they add no protection against a hostile embedder.

## Evidence bridge (synthetic studies stay hypotheses)
`StudyEvidenceRef` (`market/synthetic_research/evidence_ref.py`) is a frozen, serialisable pointer to a synthetic study (study id, manifest hash, construct, embedder name, `synthetic=True`, label `HYPOTHESIS, synthetic, unvalidated`). It serialises to a `synthetic-study:v1:` string so it fits the existing `Experiment.evidence_refs` tuple with no contract change.
* `StudyEvidenceWriter` plugs into `plan_experiments(writer=...)` and labels the proposed change; study id and construct are sanitised (no newline, control or bidi characters, brackets neutralised, 80-character cap) before they are rendered.
* `attach_study_refs` accepts a planned `Experiment` only; an `Opportunity` (whose evidence is counted) or a `CommercialHypothesis` (whose evidence is sealed) raises `TypeError`.
* `plan_experiments` does not count a `synthetic-study:` ref as a supporting observation, and `freeze_experiment` raises if a hypothesis carries one, so a synthetic ref can never enter an experiment's sealed evidence.
* `StudyEvidenceRef.parse` raises exactly `ValueError` for any malformed input; `split_refs` returns `(first-party refs, parsed synthetic refs, malformed synthetic refs)` so one bad ref never breaks a list and is never mistaken for first-party evidence.
* `FirstPartyLedger.record` refuses a non-`FirstPartyMetric`, a source outside `METRIC_SOURCES` (the empty legacy default is tolerated) and any source or provenance provider starting with `synthetic`, `ssr`, `simulated` or `study`. `METRIC_SOURCES` gains no synthetic source and `outcome_store.refuse_simulated` is unchanged.

## Validation plan (when read-only Search Console access is authorised)
Not done, and not claimed. With an approved read-only property and an approved page-text snapshotter: (1) compute relevance for the query/page pairs; (2) compare it descriptively with observed CTR and position stability over a held-out window (distribution, rank correlation, no causal language); (3) choose the map and thresholds from those data, not from this document; (4) only then consider changing the flag default. No relevance value should drive a recommendation before step 3.

## Using it
```python
from agentic_os.content.relevance import make_relevance_fn
from agentic_os.integrations.gsc import collect_search_observations

relevance_fn = make_relevance_fn(embedder, page_text_provider)       # both injected; one per collection run
observations = collect_search_observations(client, site_url, relevance_fn=relevance_fn)
```
