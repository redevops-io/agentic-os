# Safe-Learn (S2) — RESULTS

**Verdict: the safe-Learn gate PASSES all pre-registered criteria.** An auditable, support-based abstention
guard makes Learn deployable on the worlds where it had room — cutting the harmful tail below the 5% bar while
preserving most of the benefit — without regressing the near-floor worlds.

Protocol per `SAFE_LEARN_MANIFEST.md`: train seed 7 (n=2000) for lesson + guard + S1 experience; eval seeds
201/203/207 (n=300). τ swept on **pooled validation (201,203)** only, `min_support=20` fixed; **held-out =
seed 207**. Frozen model Qwen temp 0.0.

## Sealed threshold
Pooled-validation risk–coverage picked the smallest τ whose material-worse rate ≤ 5%: **τ = 0.0** — i.e. the
binding guard at this operating point is simply **abstain where training support < 20**; the reliability
filter was not needed to clear the bar (a more conservative τ is available — τ=0.5 → 0.71% material-worse at
lower coverage). Auditable and simple.

## Held-out (seed 207, n=300/world). Primary = normalized paired regret.

| World | coverage | S1 Δ vs S0 | **S2 Δ vs S0** | S1 mat-worse | **S2 mat-worse** | capture preserved | benefit 90% CI | selectivity (harm accept / abstain) |
|---|---|---|---|---|---|---|---|---|
| **Stale-Quote** (misaligned) | 0.60 | +0.089 | **+0.064** | 0.117 | **0.047** ✅ | **0.72** ✅ | [0.043, 0.086] ✅ | 0.078 / **0.175** |
| **Supplier-Invoice** (misaligned) | 0.90 | +0.153 | **+0.133** | 0.040 | **0.037** ✅ | **0.87** ✅ | [0.101, 0.167] ✅ | 0.041 / 0.035 |
| Fraud A (null, regression-check) | 0.84 | −0.017 | −0.012 | 0.043 | 0.037 | — | [−0.025, 0.000] | 0.044 / 0.041 |
| Fraud B (null, regression-check) | 0.87 | +0.003 | +0.003 | 0.057 | 0.057 | — | [−0.009, 0.016] | 0.065 / 0.000 |

## Pre-registered criteria
- **PRIMARY SAFETY** — material-worse(S2 vs S0) < 5% on each misaligned world: Stale-Quote 4.7%, Supplier 3.7% → **PASS**.
- **BENEFIT** — normalized Δ(S2 vs S0) 90% CI excludes 0: Stale-Quote [0.043, 0.086], Supplier [0.101, 0.167] → **PASS**.
- **CAPTURE PRESERVATION** — S2 retains ≥ 70% of that world's S1 lift: Stale-Quote 72%, Supplier 87% → **PASS**.
- **NON-REGRESSION** — S2 does not materially worsen Fraud A/B vs their S1 (relative, per manifest): both hold (S2 ≤ S1 harm, S2 benefit ≥ S1) → **PASS**.
- **COVERAGE** — Learn applied on 60% (Stale-Quote) to 90% (Supplier) of decisions.
- **SELECTIVITY** — on Stale-Quote the guard abstains on the **higher-harm** set (17.5% would-be-harm on abstained vs 7.8% on accepted): it identifies dangerous applications, not just random exposure.

## The architectural result
On the misaligned worlds the guard converts an average-positive-but-unsafe learner into a **deployable** one:
Stale-Quote's harmful tail drops 11.7% → 4.7% while 72% of its capture survives. This supports the restated
mechanism:

> **Learn when Experience is supported; fall back to the frozen model when it isn't.** Applicability
> confidence — here, training support — governs whether learned knowledge is safe to use; verified Experience
> alone is not enough (H3).

## Honest caveats
- **The guard's selectivity is world-dependent.** It is strongly selective on Stale-Quote (harm concentrates
  in low-support buckets) but **not on Fraud B** (harm_avoided_on_abstained = 0.0). Fraud B passes only by
  *non-regression* (S2 ≈ S1; the guard barely changes a high-coverage small-gap world), and its residual 5.7%
  material-worse is S1's own small-gap noise, which the support guard does not reduce.
- **Loss diagnosis (Stale-Quote, pooled, 92 losses):** SUPPORT_WEAKNESS 54, BUCKET_AGGREGATION 13,
  EVIDENCE_INSUFFICIENCY 4 (77% abstainable) — which is why the guard works; but ACTION_MISMATCH 16 (the model
  deviated from the lesson) + OUTCOME_VARIANCE 5 are a residual the support guard cannot address. Lesson
  *adherence* is a separate lever from lesson *support*.
- **Single held-out seed (207), n=300**; τ sealed on validation (601,203). Directional, not a tight estimate.
- **Disclosure:** the analysis script initially coded NON-REGRESSION as an absolute <5% bar; the frozen
  manifest specifies a *relative* "not worse than S1" criterion. The code was corrected to the frozen
  definition (Fraud B is 5.7% on both S1 and S2 — no regression) before this verdict. No other criterion was
  changed.

## Gate → next
The safe-Learn deployment gate is cleared on the misaligned worlds. Remaining, smaller threads before World 4:
the world-dependent selectivity (Fraud B) and the ACTION_MISMATCH residual (model not following a supported
lesson) — both point at lesson *adherence/coverage*, not at needing a fancier confidence model. World 4 (an
intermediate-gap world to test the graded H1) is now unblocked whenever we choose it.
