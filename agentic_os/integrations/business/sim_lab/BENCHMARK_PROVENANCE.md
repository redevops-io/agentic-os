# Business Simulation Lab — benchmark provenance

Honest record of how each world's numbers were produced, what was adjusted and when, and which results are
stable vs exploratory. Deterministic results are frozen in `tests/golden/`; live frozen-model results are
single-run exploratory evidence and are **not** golden.

## Frozen-model identity
Same model across every world and the Receivables track: OpenAI-compatible local endpoint, model
`qwen3.8-27b`, temperature 0.0, no weight changes. Identity = (base_url, model, temperature). Only
behavioral inputs are ever placed in a prompt (no protected traits in Fraud; no identity fields anywhere).

## Worlds
- **Fraud A** (`fraud_order_review/v1`) — card-not-present e-commerce, physical goods. Original fraud world;
  numerics unchanged by the profile refactor (golden `sim_lab_fraud.json` re-verified identical).
- **Fraud B** (`fraud_order_review/marketplace-digital/v1`) — digital-goods marketplace: higher fraud base
  rate (0.30 vs 0.18), instant delivery (a hold rarely stops fraud), thinner error margins, and **noisier
  signals** (fraud overlaps legit far more) → a higher irreducible evidence floor. Same decision family and
  observable schema as A, so A's policy can be applied to B directly.
- **Stale-Quote** (`stale_quote_followup/v1`) — contractor quote follow-up. Domain-native actions and a
  long-horizon oracle (eventual conversion, not activity).

## Pre-freeze simulator adjustment (Stale-Quote) — verbatim
Signal fidelity and the learning key were changed **once, before the golden was frozen**, because with the
initial parameters a blanket-HOLD policy dominated every learned policy (an artifact). Two changes:
(1) raised the readiness-signal fidelity (e.g. a ready lead replies ~0.72 rather than ~0.55); (2) dropped
`days_since_quote` from the learning key because it does not enter the oracle (a pure distractor that only
inflated bucket sparsity). Qualitative behaviour — strong native gap, weak zero-shot transfer, transfer as a
useful initialisation, restraint as the shared lesson — was checked **before and after** the change and held
in both. **No post-golden tuning.**

## Deterministic (golden) results
- `sim_lab_fraud.json` — Fraud A: learned bucket-policy halves regret (2197.8→1061.1) and cuts false-declines
  7.6%→2.1% vs the naive decline-on-flags baseline.
- `sim_lab_stale_quote_transfer.json` — Stale-Quote: S1 native −60% vs baseline; S2 zero-shot semantic
  transfer ≈ baseline (null); literal-threshold control beats baseline but ≪ native; S3 transfer+adapt beats
  S1 at small train budgets. Evidence floor ≈ 11.9k (the CLOSE_LOST / dead-vs-patient irreducibility).
- Fraud A→B transfer (deterministic): B_baseline 4589 → B_native 1171 (−74%); **A_to_B_direct 1321 (−71%)** →
  same-family policy transfers directly across a distribution shift (unlike cross-domain, where only the
  principle transferred); A_to_B_adapt fully recovers to native (1171).

## Live frozen-model results — EXPLORATORY (single run, non-deterministic, small n)
Receivables (n=48): no-experience regret 20,576 → +verified-experience 3,688 (~82% lower).

Prior-misalignment / evidence-floor table (n=16 eval per arm; floors on n=1500):

| World | evidence floor | model S0 | model S1 | systematic = S0−floor | observed lift |
|---|---|---|---|---|---|
| Fraud A | 482 | 544 | 2174 | ~62 (≈0) | none / negative |
| Fraud B | 541 | 459 | 527 | 0 | none |
| Stale-Quote | 13,525 | 70,803 | 13,495 | 57,278 | 57,309 (81%) |

n=16 is exploratory evidence, not a stable effect estimate; single non-deterministic run. The evidence floor
is an in-sample ceiling (bucket-oracle fit and evaluated on the same large sample) and is therefore mildly
optimistic. Directional finding — robust to that noise — below.

## Falsifiable hypothesis under test
> **H:** Runtime-learning lift is proportional not to task difficulty, but to the presence of a stable,
> learnable mismatch between the frozen model's behavioral prior and the environment's actual economics.

We do **not** fit a proportionality on three/four worlds. We measure the predictor — `systematic
misalignment = model_S0_regret − evidence_floor` — in every world, and check the ordering:

- Fraud A/B: model S0 ≈ evidence floor → systematic ≈ 0 → **no lift available** (observed null / negative).
  Fraud is not "unlearnable" (a naive baseline has a large fixable gap); rather the **frozen model's prior is
  already at the evidence floor**.
- Stale-Quote: model S0 ≫ floor → systematic large → **lift ≈ systematic**, pulling the model down to the
  floor (S1 13,495 ≈ floor 13,525). The residual after learning **is** the evidence-insufficient part.

This separates **learning failure** (fixable systematic gap) from **evidence insufficiency** (the floor). The
CLOSE_LOST/dead-vs-patient case is the canonical evidence-insufficiency example: it points to a Discovery
question (what additional observable would separate a dead lead from a patient one?), not to more Learn.
