# First-party ledger: source inventory and ingestion adapters (SR-04 round 4)

Scope: every place in this repository that constructs a `FirstPartyMetric`, calls `FirstPartyLedger.record`, names `METRIC_SOURCES`, or writes an outcome store, found by searching `agentic_os/`, `apps/`, `tests/`, `docs/`, `deploy/`, `examples/` and the sidekick / commercial bindings at head `feat/search-relevance-hook`.

## The rule (owner decision, option B)
`FirstPartyLedger.record` (`agentic_os/market/outcomes_join.py`) accepts a metric only if **all** of these hold. The guard is **unconditional**: no feature flag, and it behaves identically with SR-04 and `AGENTIC_OS_SYNTHETIC_RESEARCH` unset (it still refuses).

1. The object is exactly a `FirstPartyMetric` (no subclass, no duck type, no study or synthetic-response object, nothing carrying `synthetic=True`, nothing defined in the synthetic-research package).
2. `source` is exactly one of `METRIC_SOURCES` = `umami, gsc, marketplace, crm, billing, erp, analytics`. A missing, empty or whitespace source, a different case or spelling, and every unregistered source (`stripe`, `ga4`, `internal`, `analytics_v2`, `umami:site`, ...) is refused. There is no provider-name heuristic any more.
3. The metric carries an **adapter attestation** (`market/ingestion.py`): `HMAC(adapter key, digest of every metric field and the whole provenance)` stamped by a registered `SourceAdapter` for that source. A hand-built `FirstPartyMetric(source="umami")` has none and is refused; an attestation copied onto another metric, or invalidated by any later mutation, does not verify.
4. No field or provenance entry is a `synthetic-study:` reference (any case, any surrounding whitespace).

**Integrity and copies.** The attestation is bound to the metric's content: moving it to a different metric, or mutating a metric after it was minted, invalidates it; an exact copy of a valid metric (`copy`, `pickle`, `dataclasses.replace` without changes) keeps a valid attestation, which is by design. The ledger verifies the attestation on record and on every read (`get`, `value`, `higher_is_better`, `metrics`, iteration, `measured_metric_names`): a recorded metric that was mutated afterwards raises `IngestionRefused` instead of being served.

**Honest limit.** The guard is **bypassable by code that runs in the same process** (it can inspect the interpreter's objects, call the factory from an unrelated module or replace the module); these checks stop accidental and casual misuse. The attestation makes label-only or accidental mislabelling impossible and turns every first-party writer into an explicit, auditable registration; the signing keys live in a closure (not in instance attributes) and adapters refuse to be pickled or copied; any call that has a frame of the synthetic-research package on its stack is refused by `make_metric`, `register_source_adapter`, `repo_adapter` and `FirstPartyLedger.record`. The structural protection is the import rule: the SSR / study package (`agentic_os/market/synthetic_research`) must not import `outcomes_join`, `ingestion`, an adapter or the outcome store (`tests/test_first_party_ledger_guard.py` enforces it over every module in that package), and `METRIC_SOURCES` has no synthetic source.

## Synthetic-study references: one shared, normalising test
`agentic_os/market/synthetic_refs.py` is the single test used by the ledger guard, `freeze_experiment`, `plan_experiments`' explain counting and `evidence_ref.split/attach`. A reference is normalised first: NFKC (fullwidth forms such as `ｓｙｎｔｈｅｔｉｃ－ｓｔｕｄｙ：`), every format / control / private-use / unassigned character removed (zero-width characters, bidi controls, soft hyphen, BOM, NUL), case-folded, blanks stripped, and a deliberately partial table of Cyrillic / Greek / Armenian look-alike letters folded. Two predicates, on purpose different:

| Predicate | Rule | Used by |
|---|---|---|
| `is_synthetic_ref` | the normalised text STARTS WITH `synthetic-study:` or `synthetic_study:` | experiment counters (`plan_experiments` explain), `freeze_experiment`, `evidence_ref` split/attach |
| `mentions_synthetic_ref` | the normalised text CONTAINS either scheme ANYWHERE | the first-party ledger guard (every metric field and the provenance) |

The ledger guard is therefore **stricter than the experiment counters**, by design: `obs:synthetic-study:x` is refused by the ledger but is still an ordinary, counted reference for the experiment plane exactly as before; `synthetic-studyx`, `synthetic study:v1:x`, `synth-study:x`, `syn-thetic-study:`, `study:x` and `ssr:1` are ordinary references everywhere. What changes for the experiment plane: a reference that starts with the scheme only after normalisation (a bidi or zero-width prefix, fullwidth letters, `synthetic_study:`, a Cyrillic look-alike letter) is now recognised, so it is no longer counted as a supporting observation and `freeze_experiment` refuses it; all other references behave as on base.

## Migration path for out-of-repo callers
```python
from agentic_os.market.ingestion import register_source_adapter

adapter = register_source_adapter("analytics", "acme-warehouse-import", owner="data team")   # once, at start-up
metric = adapter.make_metric(metric="signup_conversion", value=0.12, segment="treatment", surface="pricing_page")
ledger.record(metric)
```
`register_source_adapter` accepts only the seven `METRIC_SOURCES`, refuses a repeated id and refuses ids that name a synthetic / simulated / study producer. Mapping a new external system (Stripe, GA4, an internal warehouse) onto the closest existing source is a caller decision; **adding a new source name needs a reviewed change to `METRIC_SOURCES`**. There is deliberately **no compatibility shim**: a shim that kept accepting unknown labels would silently allow unknown sources. Callers fail closed with an error that names `register_source_adapter`.

## In-repo writers (constructors and `record` callers)
| Location | What it does | Source | Owning adapter |
|---|---|---|---|
| `agentic_os/market/outcomes_join.py` `FirstPartyLedger.record` | the only ledger writer API | verifies | n/a |
| `agentic_os/market/ingestion.py` `SourceAdapter.make_metric` | the only supported factory | per adapter | the registered adapter |
| `tests/test_market_outcomes_join.py` (4 tests) | built metrics by hand with `source="umami"`; now mints through `repo_adapter("umami")` | `umami` | `repo:umami` |
| `tests/test_first_party_ledger_guard.py`, `tests/test_ledger_guard_compat.py`, `tests/ledger_compat_probe.py` | guard and compatibility tests | all seven + refused labels | `repo:<source>`, test adapters |
| `agentic_os/market/__init__.py` | re-exports `FirstPartyLedger`, `FirstPartyMetric` | n/a | n/a |

**No in-repo sensor constructs a `FirstPartyMetric` today.** The sensors that would legitimately feed the ledger produce other types: `integrations/umami.py` yields `AnalyticsObservation`s, `integrations/gsc.py` yields `SearchObservation`s, `market/marketplace.py` reads a `PricingContext`, `crm_outcome_live.py` writes `OutcomeEvent`s. `measure_experiment` / `conversion_delta` read the ledger but nothing in the repository populates it outside tests. The built-in adapter ids reserved for them (`repo_adapter(source)`, `IN_REPO_SOURCE_OWNERS` in `ingestion.py`) are:

| Source | Would be owned by | Built-in adapter id |
|---|---|---|
| `umami` | `agentic_os/integrations/umami.py` (UmamiClient) | `repo:umami` |
| `gsc` | `agentic_os/integrations/gsc.py` (GscClient) | `repo:gsc` |
| `marketplace` | `agentic_os/market/marketplace.py` | `repo:marketplace` |
| `crm` | `agentic_os/crm_outcome_live.py` | `repo:crm` |
| `billing` | `apps/billing` | `repo:billing` |
| `erp` | no in-repo producer | `repo:erp` |
| `analytics` | no in-repo producer | `repo:analytics` |

## Related but separate: outcome stores (`OutcomeEvent`, not `FirstPartyMetric`)
`OutcomeStore.append` callers: `agentic_os/projects_api.py` (lines 595, 624-627), `agentic_os/outcome_derivation.py:65`, `agentic_os/policy_promotion.py:339`, `apps/billing/decision.py` (stores), `agentic_os/crm_outcome_live.py:104` and `outcome_learning_backtest.py` (`log.record(OutcomeEvent(...))`), `agentic_os/commercial/outcome.py:57` (`reward_sink().record`). These are a different plane (priority-engine learning). They are unchanged; `outcome_store.refuse_simulated` still refuses `simulated=True` events, and the SSR / study package must not import any of them (same static test).

## Potential external callers and sources to ask the owner about
The ledger refuses these after this change unless an adapter is registered for a registered source. Nothing in this repository uses them as a `FirstPartyMetric.source`; they appear only as business-connector providers (`agentic_os/integrations/business/*`, `projects_api.py`, `connect_plan.py`) that produce other objects.
* `stripe` (payments) - closest source `billing`.
* `ga4` / Google Analytics, `analytics_v2` - closest source `analytics` (or `umami` for page behaviour).
* `umami:site` and other `umami:<site>` variants - map to `umami` and carry the site in `surface`.
* `internal` / warehouse exports - closest source depends on the metric; owner to decide.
* any caller that built `FirstPartyMetric` with the default empty `source` (the base tolerated it; now refused).
* any caller that passes duck-typed or subclassed metrics (the base accepted any object with the right attributes).

The owner is asked to confirm, for each caller that exists outside this repository, which `METRIC_SOURCES` entry it maps to, or whether a new source name should be added by a reviewed change.
