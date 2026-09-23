# Prospective-H Confirmation Battery — immutable manifest (`prospective-h-battery/v1`)

This manifest is frozen. It is the pre-registered specification of the confirmation run, fixed **before** the
run and **before** any fourth world is added. The Simulation Lab is treated as frozen at this point: the shift
is from benchmark construction to hypothesis confirmation.

## Hypothesis
> **H:** Verified Experience primarily removes *systematic* regret above the observable-information floor. It
> should reliably help exactly when the frozen model's behavioral prior is misaligned with the environment's
> economics (`systematic = model_S0 − held-out floor` is large), and should NOT reliably help when the prior
> is already near the floor (`systematic ≈ 0`).

## Frozen spec
- **Worlds** (all four current sim_lab worlds):
  - `fraud_order_review/v1` (Fraud A) — **null control**
  - `fraud_order_review/marketplace-digital/v1` (Fraud B) — **null control**
  - `stale_quote_followup/v1` — misaligned (large gap expected)
  - `supplier_invoice_control/v1` — misaligned (large gap expected)
- **Seeds:** `(11, 23, 37, 53, 71)` (5 per world)
- **n_eval:** 32 held-out cases per seed · **floor_fit_n:** 2000 (held-out observable-policy floor)
- **Frozen model:** Qwen `qwen3.8-27b`, temperature 0.0, OpenAI-compatible endpoint (same identity as every gate)
- **Pre-registered decision rule** (fixed before any world was run):
  - `CAPTURE_THRESHOLD = 0.40` — a positive-prediction world's revealed S1 must satisfy `S1 ≤ S0 − 0.40·systematic`.
  - `NEGLIGIBLE_SYSTEMATIC_SHARE = 0.15` — if `systematic < 0.15·S0`, the seed is predicted ~null and holds iff `|lift| < 0.15·S0`.
  - `seed_pass_fraction = 0.80` — a world passes iff ≥ 4/5 seeds hold their pre-registered prediction.

## Protocol (per world, per seed)
1. Estimate the held-out observable-policy floor (fit on `floor_fit_n`, evaluate on the same held-out eval as S0/S1).
2. Run **S0** (frozen model, no experience) on the eval set.
3. **Record the prediction** from `(floor, S0)` — ceiling and null/positive call — before S1 exists.
4. Reveal **S1** (frozen model + native verified experience) on the same eval set. Test the recorded prediction.

## Gate (pre-registered)

```
                 PROSPECTIVE H BATTERY
                         │
              ┌──────────┴──────────┐
        H survives              H weakens / fails
              │                     │
     add fourth axis         investigate mechanism
   (chosen to ATTACK H:      before expanding worlds
    an INTERMEDIATE gap)
              │
     replication ladder
              │
     real-data H test
```

**H SURVIVES** iff every world passes its seed bar — operationally: both null-control worlds keep ~zero lift
(≥80% of seeds hold the null prediction) **and** both misaligned worlds capture ≥40% of systematic in ≥80% of
seeds.

**Explicit falsifiers** (either one weakens H):
- **Null-control breach** — a Fraud world shows real Learn lift despite `systematic ≈ 0`.
- **Capture miss** — a large-gap world repeatedly fails the ≥40% capture threshold.

## Status
Results are recorded in `BATTERY_RESULTS_v1.md` (dated, exploratory: the frozen model is non-deterministic, so
figures are reported with mean±sd across seeds, not asserted in goldens). The deterministic battery machinery +
gate logic are covered by `tests/test_integration_business_sim_lab_battery.py`.

If H survives: the fourth axis is chosen specifically to **attack** H — a world with reason to expect an
**intermediate** systematic gap, because the current contrast is near-zero vs huge; showing the predictor
discriminates in the middle is worth more than another dramatic positive.
