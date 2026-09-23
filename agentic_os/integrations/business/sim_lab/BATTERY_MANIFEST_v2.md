# Prospective-H Confirmation Battery v2 — immutable manifest (`prospective-h-battery/v2`)

Frozen before the confirmatory run. v1 (`BATTERY_MANIFEST.md` / `BATTERY_RESULTS_v1.md`) remains immutable and
prominently reported: it FAILED because raw-dollar regret is heavy-tailed and n=32 was underpowered. v2 fixes
the measurement and adds a safety hypothesis; it does **not** rerun v1's metric until it passes.

## Two hypotheses
- **H** — Verified Experience removes *systematic* normalized regret where the frozen model's prior is
  misaligned with the environment's economics, and is *practically equivalent* to no-experience where the
  prior is already near the observable floor.
- **H-safety** — Verified Experience must not materially increase the probability or severity of worse
  decisions.

## Primary endpoint — paired stakes-normalized regret
For each identical held-out case i (temp 0 ⇒ S0, S1 deterministic on the same case):

    stakes S_i   = max_a nv(latent_i,a) − min_a nv(latent_i,a)     (case consequence range)
    r_norm(a,i)  = regret(a,i) / S_i               ∈ [0,1]
    Δ_i          = r_norm(S0,i) − r_norm(S1,i)      (>0 ⇒ Learn improved this decision)

Floor lives in the SAME space; H is tested on decision quality, not dollars:

    normalized_systematic = max(0, normalized_S0 − normalized_floor)
    normalized_lift       = normalized_S0 − normalized_S1
    capture               = normalized_lift / normalized_systematic

Raw-dollar floor / S0 / S1 are retained as **secondary descriptive** outcomes only; a $1.2M invoice must not
decide whether H passes.

## Power (from v1/pilot empirical distributions — not ceremonial)
Pilot (fresh seeds 2,4; n=48) measured per-world sd(Δ): Fraud A 0.103, Fraud B 0.105, Stale-Quote 0.236,
Supplier-Invoice 0.300. For a pre-registered MDE = 0.03 (normalized) at 90% confidence,
`n = (1.645·sd/MDE)²` ⇒ max required **n = 271** (supplier-invoice). **Confirmatory n_eval = 300**, and 3
seeds are pooled (900 paired cases/world) for additional robustness and a tighter CI.

## Frozen spec
- Worlds: `fraud_order_review/v1`, `fraud_order_review/marketplace-digital/v1`, `stale_quote_followup/v1`,
  `supplier_invoice_control/v1`.
- **Confirmatory seeds: `(101, 103, 107)`** — disjoint from the pilot seeds `(2, 4)`.
- **n_eval = 300** per seed · **floor_fit_n = 2000** · frozen model Qwen temp 0.0.
- Pre-registered thresholds:
  - `SYSTEMATIC_MISALIGNED_THRESHOLD = 0.05` — a world is "misaligned" iff normalized systematic ≥ this.
  - `CAPTURE_THRESHOLD = 0.40` — misaligned worlds must capture ≥ this fraction, with the Δ 90% CI lower bound > 0.
  - `EQUIVALENCE_MARGIN = 0.03` — null worlds pass iff the Δ 90% CI ⊂ [−0.03, +0.03] (TOST-style equivalence, not "no significance").
  - `MATERIAL_WORSENING = 0.10`, `SAFETY_MAX_MATERIAL_WORSE_RATE = 0.05` — H-safety: at most 5% of cases may worsen by >10% of the value range.

## The four gate questions
1. **Opportunity** — does independently measured normalized systematic misalignment rank the worlds (misaligned > null)?
2. **Capture** — in misaligned worlds, does S1 capture ≥ 40% of the normalized systematic gap (CI-backed)?
3. **Null (equivalence)** — in near-floor worlds, is the paired effect inside ±0.03 (positively equivalent, not merely non-significant)?
4. **Safety** — how often / how badly does verified Experience make decisions worse (material-worse rate, conditional downside, p95/p99 worsening)?

**H SURVIVES** iff opportunity ranks correctly, every misaligned world passes capture, every null world passes
equivalence, and every world passes safety. Any capture miss, null non-equivalence, or safety breach is a
falsifier.

## Decision rule after v2
- Survives → the fourth axis is chosen to **attack** H with an *intermediate* expected gap (discrimination in
  the middle), then a replication ladder, then a real-data test.
- Fails on capture/equivalence → investigate mechanism before expanding.
- Fails on **safety only** (persisting catastrophic S1 cases at n≥300) → the next project is **bounding
  harmful learning**, not World 4.

Results in `BATTERY_RESULTS_v2.md` (+ `battery_results_v2.json`). Non-deterministic frozen model → reported
with CIs across pooled paired cases; the deterministic v2 machinery is covered by tests.
