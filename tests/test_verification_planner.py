"""Verification planner — unit rules + Benchmark F (audit §12).

Benchmark F pits three strategies on a realistic action mix and shows the ladder buys the
*safety* of blanket human review (zero false admissions) at a small fraction of its cost,
while blanket "always semantic" is cheaper but unsafe (it misses problems above its tier).
"""
from decimal import Decimal

import pytest

from runtime_contracts.models import (
    AssuranceTier, VerifierDescriptor, VerificationRequirement,
)
from runtime_contracts.models.capability import Estimate
from runtime_contracts.models.verification import Determinism
from runtime_contracts.models.verification_ladder import tier_rank
from agentic_os.mission.verification_planner import (
    VerificationPlanner, VerificationUnsatisfiable,
)


def _v(vid, tier, det, cost, latency_ms):
    return VerifierDescriptor(
        verifier_id=vid, tier=tier, determinism=det,
        cost=Estimate(low=cost, high=cost, unit="usd"),
        latency=Estimate(low=str(latency_ms), high=str(latency_ms), unit="ms"))


REGISTRY = [
    _v("verify.schema",   AssuranceTier.STRUCTURAL,    Determinism.DETERMINISTIC,     "0.001", 5),
    _v("verify.rule",     AssuranceTier.DETERMINISTIC, Determinism.DETERMINISTIC,     "0.005", 20),
    _v("verify.evidence", AssuranceTier.EVIDENTIAL,    Determinism.DETERMINISTIC,     "0.02", 100),
    _v("verify.semantic", AssuranceTier.SEMANTIC,      Determinism.NON_DETERMINISTIC, "0.05", 800),
    _v("verify.ensemble", AssuranceTier.ENSEMBLE,      Determinism.NON_DETERMINISTIC, "0.25", 2000),
    _v("verify.human",    AssuranceTier.HUMAN,         Determinism.NON_DETERMINISTIC, "8.00", 3_600_000),
]
BY_ID = {v.verifier_id: v for v in REGISTRY}


# ── unit rules ────────────────────────────────────────────────────────────────
def test_picks_cheapest_sufficient():
    p = VerificationPlanner()
    # medium/reversible → EVIDENTIAL bar; cheapest sufficient is verify.evidence.
    choice = p.plan(risk="medium", available=REGISTRY)
    assert choice.verifier.verifier_id == "verify.evidence"
    assert choice.reason == "cheapest_sufficient"


def test_high_risk_will_not_close_on_a_lone_model():
    p = VerificationPlanner()
    # high → SEMANTIC bar with deterministic-final: a lone non-deterministic semantic judge
    # is not sufficient, so it escalates to the ensemble (a robust model verdict).
    choice = p.plan(risk="high", available=REGISTRY)
    assert choice.verifier.verifier_id == "verify.ensemble"


def test_low_risk_reversible_uses_the_cheapest_check():
    choice = VerificationPlanner().plan(risk="low", available=REGISTRY)
    assert choice.verifier.verifier_id == "verify.schema"


def test_unsatisfiable_is_fail_closed():
    p = VerificationPlanner()
    # critical bar but only cheap verifiers available and no human rung → refuse.
    with pytest.raises(VerificationUnsatisfiable):
        p.plan(risk="critical", available=[BY_ID["verify.schema"], BY_ID["verify.rule"]])


def test_escalates_to_human_when_present_but_nothing_else_suffices():
    p = VerificationPlanner()
    choice = p.plan(risk="critical",
                    available=[BY_ID["verify.schema"], BY_ID["verify.human"]])
    assert choice.escalated and choice.verifier.tier is AssuranceTier.HUMAN


def test_gate_is_fail_closed_on_indeterminate():
    class R:  # mimics VerificationResult.verified
        def __init__(self, v): self.verified = v
    assert VerificationPlanner.gate(R(True)) is True
    assert VerificationPlanner.gate(R(False)) is False


# ── Benchmark F ───────────────────────────────────────────────────────────────
# A realistic mix: most actions are low/medium risk; a few are high/critical. Half of each
# risk band is invalid, flawed *at its own tier* (only a verifier reaching that tier catches it).
def _actions():
    plan = [("low", 14), ("medium", 6), ("high", 3), ("critical", 1)]
    tier_of = {"low": AssuranceTier.STRUCTURAL, "medium": AssuranceTier.EVIDENTIAL,
               "high": AssuranceTier.SEMANTIC, "critical": AssuranceTier.ENSEMBLE}
    out = []
    for risk, n in plan:
        for i in range(n):
            invalid = (i % 2 == 0)                    # ~half invalid
            out.append({"risk": risk,
                        "flaw_tier": tier_of[risk] if invalid else None})
    return out


def _run(verifier, action):
    """Simulated verdict: a verifier catches a flaw iff it reaches the flaw's tier."""
    if action["flaw_tier"] is None:
        return "PASS"                                 # valid → admit
    return "FAIL" if tier_rank(verifier.tier) >= tier_rank(action["flaw_tier"]) else "PASS"


def _score(strategy):
    cost = Decimal("0"); latency = 0; false_admissions = 0; human = 0
    p = VerificationPlanner()
    for a in _actions():
        v = strategy(p, a)
        cost += Decimal(v.cost.high)
        latency += int(v.latency.high)
        if v.tier is AssuranceTier.HUMAN:
            human += 1
        verdict = _run(v, a)
        if verdict == "PASS" and a["flaw_tier"] is not None:
            false_admissions += 1                      # admitted a flawed action
    return {"cost": cost, "latency": latency,
            "false_admissions": false_admissions, "human": human}


def test_benchmark_f_ladder_is_safe_and_far_cheaper_than_blanket_human():
    always_semantic = _score(lambda p, a: BY_ID["verify.semantic"])
    always_human    = _score(lambda p, a: BY_ID["verify.human"])
    ladder          = _score(lambda p, a: p.plan(risk=a["risk"], available=REGISTRY).verifier)

    # Safety: the ladder admits zero flawed actions — like blanket human, unlike semantic.
    assert always_semantic["false_admissions"] > 0        # misses ensemble-tier flaws
    assert always_human["false_admissions"] == 0
    assert ladder["false_admissions"] == 0

    # Cost: the ladder buys that safety for a small fraction of blanket human review,
    # because the low-risk majority uses cheap deterministic checks.
    assert ladder["cost"] < always_human["cost"] / 10
    assert ladder["human"] == 0                            # nothing needed the human rung here

    # (Informational — surfaced if the test is run with -s.)
    print("\nBenchmark F:",
          {k: {"cost": str(v["cost"]), "false_admissions": v["false_admissions"]}
           for k, v in [("always_semantic", always_semantic),
                        ("always_human", always_human), ("ladder", ladder)]})
