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
    BEHAVIORAL_OBSERVABLE_KEYS, FRAUD_A, FRAUD_B, FraudProfile, FraudWorld,
    PROTECTED_TRAITS_NEVER_USED, fraud_world_a, fraud_world_b)
from .fraud_transfer import run_fraud_transfer
from .misalignment import (
    PriorMisalignment, ProspectiveHResult, WorldLearnProfile, bucket_oracle_policy, evidence_floor_regret,
    measure_misalignment, predict_learn_opportunity, profile_world, reveal_learn_outcome)
from .lessons import RECEIVABLES_INTERVENTION_LESSON, Lesson, extract_lesson
from .supplier_invoice_world import OBSERVABLE_KEYS as SUPPLIER_OBSERVABLE_KEYS
from .supplier_invoice_world import SupplierInvoiceWorld
from .confirmation_battery import (
    BATTERY_VERSION, BatterySpec, WorldBatteryResult, deterministic_battery, gate_verdict,
    run_world_battery)
from .confirmation_battery_v2 import (
    BATTERY_V2_VERSION, WorldV2Result, case_stakes, gate_v2, run_world_v2)
from .model_arm import (
    FrozenChatModel, build_experience, endpoint_reachable, run_model_experiment)
from .abstract import (
    AbstractFeatures, Posture, PostureModel, Readiness, Stakes)
from .stale_quote_world import StaleQuoteWorld
from .transfer import (
    learn_receivables_posture_policy, literal_transfer_arm, run_transfer_experiment,
    sample_efficiency_curve, transfer_adapt_arm, zero_shot_transfer_arm)
from .stale_quote_model_arm import run_s0_to_s3

__all__ = [
    # Phase 0 shared contracts
    "DecisionCase", "Proposal", "CaseOutcome", "ArmReport", "World", "RunManifest",
    "KernelResult", "apply_kernel", "digest",
    # evaluator + arms + runner
    "evaluate", "control_arm", "learned_arm", "run_world",
    # World 4 — Fraud family (profile-driven: A e-commerce, B digital marketplace) + A→B transfer
    "FraudWorld", "FraudProfile", "FRAUD_A", "FRAUD_B", "fraud_world_a", "fraud_world_b",
    "BEHAVIORAL_OBSERVABLE_KEYS", "PROTECTED_TRAITS_NEVER_USED", "run_fraud_transfer",
    # RECONCILIATION axis — Supplier Invoice Control
    "SupplierInvoiceWorld", "SUPPLIER_OBSERVABLE_KEYS",
    # prior-misalignment instrument (H) + evidence floor + PROSPECTIVE H test
    "evidence_floor_regret", "bucket_oracle_policy", "measure_misalignment", "profile_world",
    "PriorMisalignment", "WorldLearnProfile", "predict_learn_opportunity", "reveal_learn_outcome",
    "ProspectiveHResult",
    # prospective-H confirmation battery v1 (frozen manifest + gate; failed, immutable)
    "BatterySpec", "BATTERY_VERSION", "WorldBatteryResult", "run_world_battery", "gate_verdict",
    "deterministic_battery",
    # v2 — paired stakes-normalized, safety-aware
    "BATTERY_V2_VERSION", "WorldV2Result", "run_world_v2", "gate_v2", "case_stakes",
    # Lesson object (principle transfers, policy regenerates)
    "Lesson", "extract_lesson", "RECEIVABLES_INTERVENTION_LESSON",
    # real frozen-model arm (second-family same-model gate)
    "FrozenChatModel", "build_experience", "run_model_experiment", "endpoint_reachable",
    # World 2 — Stale-Quote (contractor family) + cross-domain transfer
    "StaleQuoteWorld", "AbstractFeatures", "Posture", "PostureModel", "Readiness", "Stakes",
    "learn_receivables_posture_policy", "zero_shot_transfer_arm", "transfer_adapt_arm",
    "literal_transfer_arm", "run_transfer_experiment", "sample_efficiency_curve", "run_s0_to_s3",
]
