# ACTION_MISMATCH / adherence — diagnosis (before any mechanism)

Safe-Learn (S2) answered *should this lesson apply?* (support/applicability). This diagnoses the second
condition — *will the model follow a supported lesson?* — before building anything, per the agreed sequence.
Deterministic (no new model calls beyond regenerating the temp-0 per-case actions, which reproduce exactly).

Accepted = supported bucket (support ≥ 20; the sealed guard). Data: `adherence_records.json` (Stale-Quote +
Supplier-Invoice, eval seeds 201/203/207, n=300).

## The 2×2 over accepted lesson applications

| World | accepted | ADHERED+GOOD | ADHERED+BAD | DEVIATED+GOOD | DEVIATED+BAD | deviation rate |
|---|---|---|---|---|---|---|
| Stale-Quote | 535/900 | 452 | **21** | **46** | **16** | 11.6% |
| Supplier-Invoice | 810/900 | 665 | 19 | 119 | 7 | 15.6% |

## Enforce-lesson counterfactual (normalized regret) + achievable ceiling

| World | S0 | S1 | **S2 (deployed)** | ENFORCE lesson | ORACLE conflict-handler |
|---|---|---|---|---|---|
| Stale-Quote | 0.223 | 0.125 | **0.156** | 0.143 | 0.140 |
| Supplier-Invoice | 0.215 | 0.061 | **0.080** | 0.075 | 0.069 |

| World | material-worse: S2 | ENFORCE | ORACLE | avoidable_regret | override_value |
|---|---|---|---|---|---|
| Stale-Quote | 4.1% | 2.9% | 2.4% | 0.0275 | 0.0059 |
| Supplier-Invoice | 2.9% | 2.1% | 2.1% | 0.012 | 0.0072 |

## Findings
1. **ACTION_MISMATCH is real but a minority of residual harm.** On Stale-Quote, **ADHERED+BAD (21) >
   DEVIATED+BAD (16)**: more accepted harm comes from the model *correctly following* a lesson that is wrong
   for the observable sub-state (a lesson-quality / bucket-aggregation limit) than from deviating. Adherence
   can address at most the 16.
2. **Most deviations are justified overrides** (Stale-Quote 46/62, Supplier 119/126) — the model saw
   case-specific evidence and beat the coarse lesson. Forcing the lesson would suppress these. But the
   harmful deviations carry more regret per case, so `avoidable_regret` (0.0275) is ~4.6× `override_value`
   (0.0059): on aggregate the accepted set favors the lesson.
3. **The achievable ceiling is small.** A *perfect* (oracle) conflict-handler improves mean normalized regret
   over the deployed S2 by only ~0.016 (Stale-Quote) / ~0.011 (Supplier), and **blind enforcement captures
   most of it**. A realistic model-adjudicated conflict-handler — which cannot know a priori which side is
   right (if the model knew, it would not have deviated) — would capture a fraction of that. The elaborate
   structured-conflict mechanism buys little over the simple lever.
4. **The simple lever conflicts with governance.** "Enforce the lesson on supported buckets" beats S2 on both
   regret and safety, but it makes the lesson *authoritative* and suppresses justified overrides — exactly
   what the pre-committed principle rules out ("lessons inform, not govern"). The data prices that governance
   choice: keeping the model in charge on accepted buckets costs ~0.013 normalized regret + ~1pp
   material-worse vs enforcing.

## H4 verdict — bounded ceiling; do not build the mechanism now
> **H4 (adherence):** explicit conflict handling reduces avoidable deviations without suppressing justified
> overrides.

The oracle bound shows H4's ceiling over the deployed S2 is small (~0.016 normalized) and mostly capturable
by blind enforcement, while the larger residual harm (ADHERED+BAD) is a lesson-quality problem adherence
cannot touch. Per the agreed rule — *if adherence produces little incremental value, record it and move on* —
a dedicated structured conflict-handler is **not justified**. Recorded and deferred.

If the ~1pp safety gain is ever wanted, the cheapest lever is "prefer the lesson action on high-support
buckets" — but that is a governance decision (lesson-as-authority, override suppression) for a human, not a
mechanism to add silently. The more valuable residual thread is **lesson quality on heterogeneous buckets**
(ADHERED+BAD), which points at finer bucketing / more evidence (Discovery), not at adherence.

## Next
Adherence resolved as low-incremental-value. **World 4** (an intermediate-gap world to test the graded H1) is
the next step — designed for scientific independence, with its S0 gap allowed to land where it lands.
