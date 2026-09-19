"""The experience→workflow-improvement loop, and the invariants it inherits from the Learn engine.

These mirror the Workflow-Teaching plan §22: a single repeated choice or lucky outcome never becomes a
rule; promotion needs support, confidence AND an explicit review; policy beats learned behavior; an ACTIVE
version is immutable (acceptance mints a new version); and a workflow with material UNKNOWNs cannot
activate. The confidence arithmetic is delegated to ``discovery_runtime.learn`` — asserted here rather than
reimplemented.
"""
from __future__ import annotations

import pytest

from discovery_runtime.learn import PromotionPolicy, wilson_lower_bound
from agentic_os.projects.contracts import (
    CandidateStatus,
    RuleProvenance,
    WorkflowDefinition,
    WorkflowStatus,
)
from agentic_os.projects.workflow_learning import (
    accept,
    activate,
    experience_from_choice,
    experience_from_outcome,
    propose_from_choices,
    propose_from_outcomes,
)

SCOPE = "op=resolve_contact|contacts_on_active_opportunity=1"
POLICY = PromotionPolicy(min_support=3, min_confidence=0.35, require_review=True)


def _wf(**kw) -> WorkflowDefinition:
    base = dict(project_id="proj_1", title="Inbound lead follow-up", trigger="qualified inbound lead")
    base.update(kw)
    return WorkflowDefinition(**base)


def _choices(n_prefer: int, n_other: int):
    xs = [experience_from_choice(context_signature=SCOPE, chosen_action="select_active_opportunity_contact",
                                 decision_ref=f"dec_{i}") for i in range(n_prefer)]
    xs += [experience_from_choice(context_signature=SCOPE, chosen_action="select_other_contact",
                                  decision_ref=f"deco_{i}") for i in range(n_other)]
    return xs


# ──────────────────────────── the invariant ────────────────────────────

def test_a_single_repeated_choice_is_not_a_rule():
    """One observed choice → a PROPOSED candidate that cannot be accepted (support < min, confidence low),
    even by an eager reviewer. No workflow version is minted."""
    wf = _wf()
    cands = propose_from_choices(_choices(1, 0), wf)
    top = next(c for c in cands if "active_opportunity" in c.proposed_rule.statement)
    assert top.status is CandidateStatus.PROPOSED and top.supporting == 1

    new_def, updated = accept(wf, top, reviewer="alice", policy=POLICY)
    assert new_def is None
    assert updated.status is CandidateStatus.NEEDS_MORE_EVIDENCE


def test_a_repeated_choice_promotes_only_with_review_and_mints_a_new_version():
    """Enough consistent choices make the candidate eligible; ACTIVE still needs an explicit accept, and
    acceptance produces a NEW version with the rule now HUMAN_CONFIRMED and the parent linked."""
    wf = _wf(version=3)
    cands = propose_from_choices(_choices(8, 0), wf)
    top = next(c for c in cands if "active_opportunity" in c.proposed_rule.statement)

    new_def, updated = accept(wf, top, reviewer="bob", policy=POLICY)
    assert new_def is not None
    assert updated.status is CandidateStatus.ACCEPTED
    assert new_def.version == 4 and new_def.parent_version_id == wf.workflow_id
    assert new_def.status is WorkflowStatus.READY_FOR_REVIEW
    assert new_def.rules[-1].provenance is RuleProvenance.HUMAN_CONFIRMED
    assert updated.resulting_workflow_version == new_def.workflow_id


def test_policy_beats_learned_behavior():
    """An otherwise-eligible candidate is rejected when the Governance predicate forbids it — policy is not
    model-visible context, and a veto never yields a version (§18.5/§18.6)."""
    wf = _wf()
    top = next(c for c in propose_from_choices(_choices(8, 0), wf)
               if "active_opportunity" in c.proposed_rule.statement)
    new_def, updated = accept(wf, top, reviewer="carol", policy=POLICY,
                              policy_forbids=lambda c: True)
    assert new_def is None
    assert updated.status is CandidateStatus.REJECTED
    assert "policy" in updated.rationale


def test_active_version_is_immutable():
    """Acceptance never mutates the input workflow; it returns a distinct new object."""
    wf = _wf(version=2)
    before_rules = wf.rules
    top = next(c for c in propose_from_choices(_choices(8, 0), wf)
               if "active_opportunity" in c.proposed_rule.statement)
    new_def, _ = accept(wf, top, reviewer="dave", policy=POLICY)
    assert new_def is not wf
    assert wf.version == 2 and wf.rules == before_rules            # untouched
    assert new_def.version == 3 and len(new_def.rules) == len(before_rules) + 1


def test_a_dominated_choice_does_not_promote():
    """When the operator mostly chose the other action, the minority choice is not eligible (its
    counterexamples dominate) — the balance of evidence gates it, not the last observation."""
    wf = _wf()
    top = next(c for c in propose_from_choices(_choices(1, 8), wf)
               if "active_opportunity" in c.proposed_rule.statement)
    new_def, updated = accept(wf, top, reviewer="erin", policy=POLICY)
    assert new_def is None and updated.status is CandidateStatus.NEEDS_MORE_EVIDENCE


# ──────────────────────────── outcome-driven (§20) ────────────────────────────

def test_outcome_learning_promotes_a_reliable_action():
    """An action that reliably produced good outcomes becomes an eligible candidate."""
    wf = _wf()
    xs = [experience_from_outcome(context_signature="channel=email|segment=smb", decision="send",
                                  action="send_short_followup", good=True, mission_ref=f"m_{i}")
          for i in range(6)]
    top = next(c for c in propose_from_outcomes(xs, wf) if c.supporting >= 3)
    new_def, updated = accept(wf, top, reviewer="frank", policy=POLICY)
    assert new_def is not None and updated.status is CandidateStatus.ACCEPTED


def test_confidence_is_the_engine_wilson_bound():
    """Sanity: the candidate's confidence is the engine's Wilson lower bound over its counts — this module
    delegates the arithmetic, it does not invent it."""
    wf = _wf()
    top = next(c for c in propose_from_choices(_choices(6, 2), wf)
               if "active_opportunity" in c.proposed_rule.statement)
    assert top.supporting == 6 and top.counterexamples == 2
    assert abs(top.confidence - wilson_lower_bound(6, 8)) < 1e-9


# ──────────────────────────── activation guard (§14) ────────────────────────────

def test_unknowns_block_activation():
    wf = _wf(status=WorkflowStatus.READY_FOR_REVIEW,
             unresolved_questions=("What if no verified email exists?",))
    with pytest.raises(ValueError):
        activate(wf, actor="grace")
    forced = activate(wf, actor="grace", allow_unknowns=True)
    assert forced.status is WorkflowStatus.ACTIVE


def test_clean_workflow_activates():
    wf = _wf(status=WorkflowStatus.READY_FOR_REVIEW)
    assert activate(wf, actor="heidi").status is WorkflowStatus.ACTIVE
