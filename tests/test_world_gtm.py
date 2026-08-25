"""GTM dogfood world — 'Find Companies Rebuilding the Runtime' (the pilot-lead workflow, public-safe)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_contracts import AuthorityContext, PrincipalRef  # noqa: E402
from agentic_os.world import (  # noqa: E402
    ALL_WORLDS,
    FindCompaniesRebuildingTheRuntime,
    ScenarioOrchestrator,
)

_APPROVE = {"Approve outreach to acme-ai-platform?": "yes"}


def _auth():
    return AuthorityContext(authority_id="ctx", principal=PrincipalRef(id="founder", tenant="redevops"),
                            purpose="gtm", scope=("read:crm", "write:crm"))


def _run(**kw):
    return ScenarioOrchestrator().run(FindCompaniesRebuildingTheRuntime(), seed="c1", authority=_auth(),
                                      offline=True, **kw)


def test_discovers_scores_and_qualifies_on_evidence():
    run = _run(answers=_APPROVE)
    o = run.outcome
    assert run.metrics.verified and o["qualified"] >= 1
    # every pipeline item carries a Runtime Fit score, a tier, and a mapped pilot
    for s in o["pipeline"]:
        assert 0.0 <= s["runtime_fit"] <= 1.0 and s["tier"] in ("P0", "P1", "P2", "P3", "Archive")
        assert s["pilot"]
    top = o["top_opportunity"]
    assert top["tier"] in ("P0", "P1")
    # a sandbox/permission/governance signal maps to the Governed Agent Execution pilot (priority)
    assert top["pilot"] == "Governed Agent Execution Pilot"
    assert "permissions" in top["components"] and top["runtime_fit"] >= 0.8


def test_crm_reconciled_no_duplicate_across_three_crms():
    top = _run(answers=_APPROVE).outcome["top_opportunity"]
    crm = top["crm"]
    assert crm["duplicate"] is False
    assert crm["salesforce_id"] and crm["hubspot_id"] and crm["redevops_crm_id"]


def test_outreach_is_drafted_grounded_and_never_auto_sent():
    run = _run(answers=_APPROVE)
    o = run.outcome
    # public-safe: held at approval, nothing sent — even with an approval answer
    assert o["approval_gated"] is True and o["sent"] is False
    assert o["draft_id"] and o["draft_id"].startswith("sim-draft-")
    # the approval was a NeedsYou / human decision
    assert any(n["reason"] == "POLICY_APPROVAL" for n in run.needs_you)
    # the draft milestone cites the pilot; the outbound-held milestone is public-safe
    labels = [m.label for m in run.trace.milestones]
    assert any("held for founder approval" in l for l in labels)


def test_attention_queue_items_created():
    o = _run(answers=_APPROVE).outcome
    assert any("need review" in a for a in o["attention"])
    assert any("needs approval" in a for a in o["attention"])


def test_signals_realism_labelled_synthetic_offline():
    # offline → fixture → SYNTHETIC (never presented as live)
    assert _run().outcome["signals_realism"] == "SYNTHETIC"


def test_ground_truth_requires_dedup_pilot_and_no_send():
    run = _run(answers=_APPROVE)
    # the world's own ground-truth check passed (verified stayed True through the orchestrator)
    assert run.metrics.verified is True


def test_registered_in_all_worlds():
    assert "gtm-pilot-discovery" in ALL_WORLDS
    d = ALL_WORLDS["gtm-pilot-discovery"].descriptor()
    assert d.realism == "REAL-LIVE" and "GitHub repository search" in d.datasources
