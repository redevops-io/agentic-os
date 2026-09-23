# Prospective-H Confirmation Battery — RESULTS (`prospective-h-battery/v1`)

**Verdict: the gate did NOT survive. Every world failed its seed-pass bar. This is the confirmation run
doing its job — it caught that the earlier single-run confirmations were not stable.**

Frozen model (Qwen, temp 0.0), 5 seeds `(11,23,37,53,71)`, n_eval 32, held-out floor (fit 2000). Single
battery run; exploratory.

## Per-world (mean ± sd across 5 seeds)

| World | mean floor | mean S0 | mean S1 | mean systematic | predict-null seeds | held seeds | PASS |
|---|---|---|---|---|---|---|---|
| Fraud A (null-control) | 364 | 512 ± 41 | 588 ± 329 | 147 | 2/5 | 2/5 | ❌ |
| Fraud B (null-control) | 562 | 599 ± 165 | 561 ± 62 | 119 | 3/5 | 3/5 | ❌ |
| Stale-Quote (misaligned) | 9,930 | 49,971 ± 18,899 | 31,306 ± 16,332 | 40,041 | 0/5 | 3/5 | ❌ |
| Supplier-Invoice (misaligned) | 87 | 998 ± 1,126 | 3,065 ± 4,985 | 911 | 0/5 | 2/5 | ❌ |

Falsifiers tripped: a null-control breach (Fraud B showed lift on 2/5 seeds despite systematic≈0) and
capture misses on all three positive-prediction worlds (held < 80% of seeds).

## The single-run results did NOT replicate
The earlier exploratory figures were rare draws, not stable effects:
- **Supplier-Invoice** earlier: S0 7,980 → S1 158, capture 0.995. Across 5 seeds S0 is usually ~400–500
  (systematic only ~300–400), and S1 swings from 136 to **13,002** — verified experience sometimes made the
  frozen model *catastrophically worse* on a rare high-value case. The earlier S0=7,980 was a single unlucky
  high-value-case draw.
- **Stale-Quote** earlier: S0 70,803 → S1 13,495 (reached floor). Across 5 seeds S0 ranges 14,280–69,802 and
  S1 4,598–48,534; lift is positive every seed (Learn consistently helped) but meets the 40% capture bar in
  only 3/5.
- **Fraud A/B**: systematic is small (0–423) and the seed-to-seed lift noise (±150–650) is comparable to it,
  so "null" is unstable — not a clean null either.

## Mechanism — this is a MEASUREMENT failure, not (yet) a verdict on H
Regret is heavy-tailed: a single mishandled high-value case (invoices to $1.2M, jobs to $60k, duplicates =
full amount) swamps 31 others. Deterministic diagnostic (fixed learned policy, no model, temp-0 so no model
noise):

| World | top-5% share of total regret | CV of mean regret @ n=32 | @ n=400 |
|---|---|---|---|
| Fraud A | 0.34 | 0.55 | 0.16 |
| Stale-Quote | 0.64 | 0.54 | 0.16 |
| Supplier-Invoice | 0.28 | 0.32 | 0.09 |

At n=32 a single mean-regret estimate carries 30–55% relative noise from **case sampling alone**. The v1
battery was underpowered for this metric; it cannot resolve the effect it was built to test. The dramatic
single-run captures were within that noise band.

## What actually survives
- **Stale-Quote**: Learn reduced regret in all 5 seeds (lift 5,493–40,623). A consistent positive direction,
  magnitude noisy — the most robust positive signal.
- **Fraud**: near-floor S0 with small systematic and near-zero (noisy) lift — consistent with the null, but
  not cleanly established at this power.
- **Supplier-Invoice**: no reliable signal at this n; experience helped on some seeds and did serious harm on
  others. The **non-monotonicity of Learn** (it can make the frozen model worse) is a real finding the lucky
  single run had hidden.

## Gate decision → LEFT branch: investigate mechanism before expanding
Per `BATTERY_MANIFEST.md`, a weakened/failed gate means **do not add a fourth world**. The mechanism is
identified: heavy-tailed regret + underpowered n. The claim from the earlier turns — that a pre-registered
predictor cleanly separates near-null from large-capture worlds — is **not supported at this power** and the
single-run evidence was over-stated.

## Proposed v2 (a NEW pre-registration, not a rerun-until-pass of v1)
1. **Variance-robust metric**: normalise per-case regret by case stakes (optimal-value magnitude), so a $1.2M
   case cannot dominate a $50k one; report the normalised mean plus a **paired** statistic — since S0 and S1
   see identical cases at temp 0, per-case `regret_S0 − regret_S1` cancels case-difficulty variance — and a
   per-case **win-rate** (share of cases S1 ≤ S0) with a CI.
2. **Power**: n_eval ≥ 400 per seed (CV ≈ 0.1) and ≥ 5 seeds.
3. **Explicitly test Learn non-monotonicity**: rate of cases where S1 is materially worse than S0.
4. Re-freeze as `prospective-h-battery/v2` and re-run before any fourth world.

v1 stands as recorded: a pre-registered test that failed and prevented an over-claim.
