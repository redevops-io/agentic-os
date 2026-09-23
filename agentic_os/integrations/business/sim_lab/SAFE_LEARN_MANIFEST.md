# Safe-Learn (S2) experiment — immutable manifest

Frozen before held-out results exist. Builds on the v2 finding: H's opportunity+capture core is confirmed,
but Learn is unsafe on Stale-Quote (19.3% of decisions materially worse). This experiment tests whether an
auditable abstention guard makes the lift deployable, without modifying S0/S1.

## Arms (S0/S1 preserved; S2 added)
- **S0** — frozen model, no Experience.
- **S1** — frozen model + Experience (current Learn).
- **S2** — S1's action where the guard ACCEPTS the lesson, else fall back to S0's action.
  S2 chooses between two already-evaluated actions, so its normalized regret is `s1_rn` on accepted cases and
  `s0_rn` on abstained ones — no extra model calls.

## The guard (deterministic, auditable — not a learned confidence model)
Reads only training Experience for a case's observable bucket b:
- `support(b)` = training cases in b
- `reliability(b)` = P(lesson_action(b) == per-case optimal | training cases in b)  (= 1 − counterexample rate)

ACCEPT iff `support(b) ≥ min_support AND reliability(b) ≥ τ`. **`min_support = 20` is fixed** (a support
floor, not swept). **τ is the single swept threshold.**

## Protocol
1. Training partition: seed 7, n=2000 (lesson, reliability map, and S1's experience all share it).
2. Eval seeds 201/203/207, n=300 each. **Validation = seeds 201, 203; held-out = seed 207** (disjoint).
3. Build the per-world reliability map from training (deterministic, no model).
4. **Sweep τ on POOLED validation only** (all worlds), producing a risk–coverage curve
   (coverage → benefit vs S0 → material-worse(S2 vs S0)). **Seal one τ**: the smallest τ (max coverage) whose
   pooled validation material-worse rate ≤ 5%.
5. **Reveal held-out**: apply the sealed (min_support, τ) to held-out per world; report S0/S1/S2.

## Pre-registered success criteria (a change to the Learn mechanism must clear all)
- **PRIMARY SAFETY** — material-worse(S2 vs S0) < 5% on each misaligned world.
- **BENEFIT** — normalized paired Δ(S2 vs S0) > 0 with 90% CI excluding 0 on each misaligned world (else the
  trivial "always fall back" wins).
- **CAPTURE PRESERVATION** — S2 retains ≥ 70% of **that world's** S1 lift (not a cross-world blend).
- **NON-REGRESSION** — S2 does not materially worsen Fraud A/B or Supplier-Invoice vs their S0/S1.
- **COVERAGE** — report the fraction of decisions on which Learn is actually applied.
- **SELECTIVITY** — material-worse(S1 vs S0) among ACCEPTED vs among ABSTAINED cases; the guard must abstain
  on the higher-harm set (identifying dangerous applications, not merely reducing exposure at random).

## Outcomes (both valuable)
- **Guard succeeds** ⇒ "Learn when Experience is supported; fall back to the frozen model when it isn't" —
  applicability confidence, not just verified Experience, governs safe use. Then the risk–coverage curve is a
  first-class architectural result.
- **Guard fails** (harm < 5% only by destroying the benefit) ⇒ harmful and useful applications are **not
  separable** from the current evidence ⇒ the next work is Discovery / evidence acquisition, not fancier
  confidence heuristics.

## Restated hypothesis
- **H1 Opportunity** — the independently estimated systematic gap (model_S0 − observable-policy floor)
  predicts the opportunity available to verified Experience. (graded: 0 → small → large gaps ⇒ none → small →
  large opportunity; Fraud B fills the middle.)
- **H2 Capture** — Learn captures a substantial fraction of that gap.
- **H3 Safety** — positive average capture does NOT imply safe per-decision improvement; learned knowledge
  requires applicability-aware abstention. This experiment tests H3 directly.

Results: `SAFE_LEARN_RESULTS.md` (+ `safe_learn_results.json`). No World 4 until this gate resolves.
