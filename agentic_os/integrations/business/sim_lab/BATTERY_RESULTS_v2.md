# Prospective-H Confirmation Battery v2 — RESULTS (`prospective-h-battery/v2`)

Variance-robust, paired, safety-aware. Powered from the v1/pilot distributions (n_eval 300, ≥ required 271;
seeds 101/103/107 disjoint from the pilot; 900 pooled paired cases per world). Frozen model Qwen, temp 0.0.
Unlike v1 this is **signal, not noise**: the normalized metric is bounded so its mean is stable, and the CIs
below are tight.

**Verdict: the gate did NOT survive — but for a real, valuable reason this time. The opportunity+capture core
of H is CONFIRMED at proper power; the gate fails on (a) a small non-null lift in Fraud B and (b) a genuine
Learn-SAFETY breach in Stale-Quote.**

## The four questions

| # | Question | Result |
|---|---|---|
| 1 | **Opportunity** — does normalized systematic rank the worlds? | ✅ **YES** — misaligned (stale 0.118, supplier 0.171) > null (fraud A 0.000, fraud B 0.020) |
| 2 | **Capture** — misaligned worlds capture ≥40% (CI-backed)? | ✅ **YES** — stale 0.50, supplier 0.85; both Δ CI90 lower bounds > 0 |
| 3 | **Null equivalence** — near-floor worlds within ±0.03? | ⚠️ **MIXED** — Fraud A equivalent (Δ CI −0.006..0.003); **Fraud B NOT** (Δ CI 0.016..0.036) |
| 4 | **Safety** — does Learn avoid materially worse decisions? | ❌ **NO** — **Stale-Quote worsens 19.3% of cases** (bar 5%); others safe |

## Per world (primary = normalized paired Δ; dollars are secondary/descriptive)

| World | norm S0 | norm floor | norm S1 | systematic | capture | mean Δ (90% CI) | win/tie/loss | material-worse | verdict |
|---|---|---|---|---|---|---|---|---|---|
| Fraud A (null) | 0.034 | 0.035 | 0.035 | 0.000 | — | −0.001 (−0.006, 0.003) | .08/.91/.01 | 0.012 | equivalent ✅, safe ✅ |
| Fraud B (null) | 0.076 | 0.056 | 0.051 | 0.020 | — | +0.025 (0.016, 0.036) | .04/.86/.10 | 0.028 | **non-equivalent** ⚠️, safe ✅ |
| Stale-Quote (misaligned) | 0.218 | 0.099 | 0.159 | 0.118 | **0.50** | +0.059 (0.046, 0.073) | .36/.35/.29 | **0.193** | capture ✅, **safety ✗** |
| Supplier-Invoice (misaligned) | 0.230 | 0.059 | 0.085 | 0.171 | **0.85** | +0.145 (0.125, 0.165) | .39/.51/.10 | 0.031 | capture ✅, safe ✅ |

## What v2 establishes
1. **The predictive core of H holds, properly powered.** Independently measured normalized systematic
   misalignment ranks the worlds, and where it is large, verified Experience captures ≥40% of it with tight
   CIs (stale 50%, supplier 85%). This is real confirmation of *opportunity + capture* on a decision-quality
   endpoint — not the heavy-tailed dollar aggregate that made v1 uninterpretable.
2. **"Null" is really "small-gap".** Fraud A is a clean null (systematic 0.000, Δ≈0, equivalent). Fraud B has
   a *small real* gap (systematic 0.020) and a matching *small real* capture (Δ +0.025) — it fails strict
   ±0.03 equivalence but is consistent with H's monotone spirit (small gap → small lift). H should be
   restated with a graded predictor rather than a binary null.
3. **Learn is not safe on Stale-Quote.** Mean Δ is positive (+0.059) and it captures 50% of systematic, yet it
   makes **19.3% of decisions materially worse** and loses outright on **29%**. An average-positive learner
   that frequently converts a fine decision into a much worse one is **not production-ready**. This is the
   H-safety hypothesis failing — and it was *invisible* in v1's dollar averages.
4. **v1's "catastrophic Supplier-Invoice S1" was a heavy-tail artifact.** In the bounded metric Supplier-
   Invoice is the *cleanest* positive (capture 0.85, only 10% losses, material-worse 3.1%, safe). The genuine
   safety problem is Stale-Quote, which the raw-dollar view had masked.

## Decision (per `BATTERY_MANIFEST_v2.md`)
The gate fails, dominated by a **safety** breach (not a measurement artifact, not a capture failure). Per the
pre-registered rule: **the next project is bounding harmful learning, not World 4.** Specifically:
- Diagnose the Stale-Quote 29% losses: are they a few bad buckets, aggregation error (per-bucket best action
  applied to a mismatched latent), or genuine negative transfer under the long-horizon fatigue dynamics?
- Add a **safe-Learn** guard (e.g., only apply a lesson where verified support is strong / abstain to the
  no-experience action otherwise) and re-measure the material-worse rate.
- Restate H with a graded systematic predictor (Fraud B) rather than a binary null.

v1 remains immutable (`BATTERY_RESULTS_v1.md`): the failed underpowered run that forced this variance-robust,
safety-aware protocol. v2 is the confirmation the benchmark existed to produce — it confirms the core and
catches a real safety flaw before any product claim.
