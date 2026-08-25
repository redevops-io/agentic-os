"""World-runtime engine: orchestrator, fabric, simulator, seeder, flagship, baselines, failure lab."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_contracts import AuthorityContext, PrincipalRef  # noqa: E402
from agentic_os.world import (  # noqa: E402
    ALL_WORLDS,
    AfterHoursLeadWorld,
    BenchmarkRunner,
    ExecutionMode,
    FinanceLeakageWorld,
    KycOnboardingWorld,
    Perturbation,
    PerturbationKind,
    ScenarioOrchestrator,
)


def _auth(scopes=("read:crm", "read:geo", "write:quote", "write:crm", "read:secrets",
                  "write:vendor", "write:billing")):
    return AuthorityContext(authority_id="ctx", principal=PrincipalRef(id="op", tenant="demo"),
                            purpose="run", scope=tuple(scopes))


def test_flagship_produces_a_verified_quote_during_the_interaction():
    run = ScenarioOrchestrator().run(AfterHoursLeadWorld(), seed="8842", authority=_auth(),
                                     answers={"What is the roof pitch?": "6/12"})
    assert run.ok and run.metrics.verified
    assert run.outcome["amount"] == 14300.0
    assert run.metrics.time_to_outcome_s <= 20            # quote during the interaction
    assert run.metrics.unsupported_guesses == 0
    # exactly one human decision (the single unresolved fact), asked not guessed
    assert len(run.needs_you) == 1 and run.needs_you[0]["reason"] == "MISSING_EVIDENCE"
    # the trace carries block transitions + a NeedsYou milestone + the moving capsule
    kinds = [m.kind for m in run.trace.milestones]
    assert "discovery" in kinds and "needs_you" in kinds and "verify" in kinds
    ny = [m for m in run.trace.milestones if m.kind == "needs_you"][0]
    assert ny.capsule.entity_id == "cust-acme" and ny.capsule.evidence_hash


def test_writes_are_simulated_never_live_under_simulate_mode():
    run = ScenarioOrchestrator().run(AfterHoursLeadWorld(), seed="s", authority=_auth(),
                                     answers={"What is the roof pitch?": "6/12"})
    # the quote + opportunity are simulated artifacts, explicitly labelled
    assert run.outcome["quote_id"].startswith("sim-quote-")
    assert run.outcome["opportunity_id"].startswith("sim-opportunity-")


def test_parks_on_missing_evidence_when_no_answer():
    run = ScenarioOrchestrator().run(AfterHoursLeadWorld(), seed="s", authority=_auth(), answers={})
    assert run.outcome.get("parked") is True and not run.metrics.verified
    assert run.needs_you and run.needs_you[0]["reason"] == "MISSING_EVIDENCE"


def test_identity_preserved_across_app_projections():
    orch = ScenarioOrchestrator()
    run = orch.run(AfterHoursLeadWorld(), seed="s", authority=_auth(),
                   answers={"What is the roof pitch?": "6/12"})
    # the seeder projected the customer into the apps and registered one identity
    g = orch._seeder  # projection store
    assert g.record("twenty", "twenty-customer-cust-acme")["entity_id"] == "cust-acme"
    assert g.record("erpnext", "erpnext-customer-cust-acme")["entity_id"] == "cust-acme"


def test_replay_is_deterministic():
    orch = ScenarioOrchestrator()
    a = orch.run(AfterHoursLeadWorld(), seed="8842", authority=_auth(),
                 answers={"What is the roof pitch?": "6/12"})
    b = orch.replay(AfterHoursLeadWorld(), a, authority=_auth(),
                    answers={"What is the roof pitch?": "6/12"})
    assert a.trace.canonical_form() == b.trace.canonical_form()
    assert a.outcome == b.outcome


def test_benchmark_runtime_beats_naive_and_manual():
    card = BenchmarkRunner().run(AfterHoursLeadWorld(), seed="8842", authority=_auth(),
                                 answers={"What is the roof pitch?": "6/12"})
    arms = {a.arm: a for a in card.arms}
    assert card.ground_truth_met is True
    assert arms["full_runtime"].outcome_reached and arms["full_runtime"].metrics.verified
    assert not arms["naive_agent"].outcome_reached                 # guessed → fails ground truth
    assert arms["naive_agent"].metrics.unsupported_guesses == 1
    assert not arms["manual"].outcome_reached
    assert arms["manual"].metrics.time_to_outcome_s > arms["full_runtime"].metrics.time_to_outcome_s


def test_failure_lab_capability_loss_stops_safely():
    run = ScenarioOrchestrator().run(AfterHoursLeadWorld(), seed="s", authority=_auth(),
                                     answers={"What is the roof pitch?": "6/12"},
                                     perturbations=[Perturbation(PerturbationKind.CAPABILITY_LOSS, "pricing.quote")])
    assert not run.ok and "unavailable" in run.safe_stop_reason      # never silently corrupts state
    assert not run.metrics.verified


def test_kyc_world_go_and_no_go():
    orch = ScenarioOrchestrator()
    go = orch.run(KycOnboardingWorld(), seed="clean", authority=_auth())
    nogo = orch.run(KycOnboardingWorld(), seed="dirty-hit", authority=_auth())
    assert go.outcome["decision"] == "GO"
    assert nogo.outcome["decision"] == "NO-GO" and nogo.metrics.policy_blocks == 1


def test_finance_leakage_recovers_and_reconciles():
    run = ScenarioOrchestrator().run(FinanceLeakageWorld(), seed="4471", authority=_auth(),
                                     answers={"Approve invoice correction?": "yes"})
    assert run.metrics.verified and run.outcome["recovered"] == 500


def test_all_worlds_registered():
    assert set(ALL_WORLDS) == {"after-hours-lead", "gtm-pilot-discovery", "creator-sponsorship", "kyc-ownership", "finance-leakage"}
    for w in ALL_WORLDS.values():
        assert w.descriptor().world_id == w.world_id       # descriptor consistent
