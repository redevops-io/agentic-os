"""Business Simulation Lab — the breadth / transfer / mechanism benchmark.

Implements ~/Documents/REDEVOPS_REAL_BUSINESS_SIMULATION_AND_TEST_PLAN.md as the *complement* to the
Receivables track (plan §30): Receivables stays the first real-data / real-model gate; the Simulation Lab
tests whether **one Runtime architecture improves the same frozen model across genuinely different business
decision families** (plan §31, Paper 1), before any live distribution.

What already existed and is reused (not rebuilt):
  * the §6 canonical state-transition kernel  → ``..transitions`` (receivables specialization). The general
    §6 kernel here (:func:`harness.apply_kernel`) enforces the SAME three invariants over any world's state:
    INITIAL_STATE_ADMISSIBILITY · ACTION_ADMISSIBILITY · TRAJECTORY_CONTRACT_CONSISTENCY. A rejected,
    inadmissible or failed action never executes → never evolves state → never becomes Experience (§2.3).
  * the A→H arm ladder + regret / unnecessary-intervention metric → the Receivables benchmark pattern.
  * the real frozen-model client (OpenAI-compatible Qwen) → ``..receivables_model_arm``.

This first slice ships Phase 0 (frozen shared contracts) + the Fraud world (plan World 4) — a decision
family (APPROVE / verify / review / DECLINE under false-positive economics) that is genuinely unlike the
receivables follow-up decision, so a win here is evidence of breadth, not of the same problem twice.
"""
from .harness import (
    ArmReport, CaseOutcome, DecisionCase, KernelResult, Proposal, RunManifest, World, apply_kernel,
    control_arm, digest, evaluate, learned_arm, run_world)
from .fraud_world import (
    BEHAVIORAL_OBSERVABLE_KEYS, FraudWorld, PROTECTED_TRAITS_NEVER_USED)
from .model_arm import (
    FrozenChatModel, build_experience, endpoint_reachable, run_model_experiment)

__all__ = [
    # Phase 0 shared contracts
    "DecisionCase", "Proposal", "CaseOutcome", "ArmReport", "World", "RunManifest",
    "KernelResult", "apply_kernel", "digest",
    # evaluator + arms + runner
    "evaluate", "control_arm", "learned_arm", "run_world",
    # World 4 — Fraud family
    "FraudWorld", "BEHAVIORAL_OBSERVABLE_KEYS", "PROTECTED_TRAITS_NEVER_USED",
    # real frozen-model arm (second-family same-model gate)
    "FrozenChatModel", "build_experience", "run_model_experiment", "endpoint_reachable",
]
