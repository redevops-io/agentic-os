"""Demonstration → WorkflowCandidate discovery (Workflow-Teaching plan §5–§9, §22).

Mirrors the plan's §22 discovery tests: repeated raw actions collapse into semantic operations; decisions
are separated from actions; observed/inferred/unknown is preserved; a completion condition is never
invented; a demonstrated UI interaction resolves to a provider API when one exists (UI Agent otherwise);
external actions become human gates; and a candidate is never executable — it becomes a DRAFT /
NEEDS_CLARIFICATION WorkflowDefinition that the existing activation gate still blocks on unknowns.
"""
from __future__ import annotations

import pytest

from agentic_os.projects.contracts import (
    DemonstrationObservation,
    DemonstrationSession,
    HumanGate,
    ObservationKind,
    RuleProvenance,
    WorkflowStatus,
)
from agentic_os.projects.workflow_discovery import candidate_to_definition, discover_workflow
from agentic_os.projects.workflow_learning import activate

K = ObservationKind


def _obs(seq, kind, intent="", **kw):
    return DemonstrationObservation(session_id="demo_1", seq=seq, kind=kind, intent=intent, **kw)


def _lead_session(with_result=True):
    obs = [
        _obs(1, K.NAVIGATION, app_id="twenty"),
        _obs(2, K.INPUT, "resolve company", target="Acme Inc", app_id="twenty"),
        _obs(3, K.READ, "resolve company", app_id="twenty", fields_read=("company",)),
        # a hidden decision: two contacts, operator picked the one on the active opportunity (§9)
        _obs(4, K.SELECTION, "select contact", target="contact_on_active_opp",
             alternatives=("other_contact",), app_id="twenty"),
        _obs(5, K.WRITE, "update crm opportunity", app_id="twenty", api_ref="twenty.api/opps/1",
             fields_written=("opportunity.contact",)),
        _obs(6, K.INPUT, "draft follow-up", app_id="mail"),
        _obs(7, K.SUBMIT, "send follow-up email", app_id="mail"),   # external comms → gate
    ]
    if with_result:
        obs.append(_obs(8, K.RESULT, "follow-up sent", app_id="mail"))
    return DemonstrationSession(project_id="proj_1", title="Inbound lead follow-up",
                                app_ids=("twenty", "mail"), observations=tuple(obs))


CAPS = {
    "resolve company": ("twenty.crm.search_company", ("ui_agent",)),
    "update crm opportunity": ("twenty.crm.upsert_opportunity", ("ui_agent",)),
    "select contact": ("twenty.crm.pick_contact", ()),
    "draft follow-up": ("content.draft", ()),
    "send follow-up email": ("mail.send", ("ui_agent",)),
}


def test_semantic_steps_not_click_scripts():
    """Consecutive observations on one intent collapse into a single semantic step (§5)."""
    cand = discover_workflow(_lead_session(), capabilities=CAPS)
    intents = [s.intent for s in cand.steps]
    # "resolve company" spanned INPUT+READteps (seq 2,3) → one step, not two.
    assert intents.count("resolve company") == 1
    assert "update crm opportunity" in intents and "send follow-up email" in intents


def test_capability_resolution_prefers_api_over_ui():
    """A demonstrated UI interaction resolves to a provider API when one exists; UI Agent is the fallback
    (§8) — the transport is chosen by capability, not dictated by the recording."""
    cand = discover_workflow(_lead_session(), capabilities=CAPS)
    upsert = next(s for s in cand.steps if s.intent == "update crm opportunity")
    assert upsert.preferred_capability == "twenty.crm.upsert_opportunity"
    assert "ui_agent" in upsert.fallback_capabilities
    assert any("update crm opportunity → twenty.crm.upsert_opportunity" in m for m in cand.capability_matches)


def test_unmatched_capability_is_unsupported_not_invented():
    """An intent with no installed capability is flagged UNSUPPORTED with a question, not silently run."""
    caps = {k: v for k, v in CAPS.items() if k != "send follow-up email"}
    cand = discover_workflow(_lead_session(), capabilities=caps)
    assert "send follow-up email" in cand.unsupported_steps
    assert any("send follow-up email" in q for q in cand.unresolved_questions)


def test_hidden_decision_becomes_a_question_not_a_silent_rule():
    """A SELECTION with alternatives is lifted into an INFERRED decision rule AND an unresolved question
    (§9) — the choice is never silently promoted."""
    cand = discover_workflow(_lead_session(), capabilities=CAPS)
    pick = next(s for s in cand.steps if s.intent == "select contact")
    assert pick.provenance is RuleProvenance.INFERRED and pick.decision_rule
    assert any("chose" in q and "Apply always" in q for q in cand.unresolved_questions)


def test_external_send_is_gated():
    """An external comms action becomes a G4 human gate — observation does not authorize repetition (§18.3)."""
    cand = discover_workflow(_lead_session(), capabilities=CAPS)
    send = next(s for s in cand.steps if s.intent == "send follow-up email")
    assert send.human_gate is HumanGate.G4_EXTERNAL_COMMS
    assert HumanGate.G4_EXTERNAL_COMMS in cand.proposed_human_gates


def test_completion_condition_is_not_invented():
    """With no observed RESULT, discovery raises a completion question rather than inventing one (§22)."""
    cand = discover_workflow(_lead_session(with_result=False), capabilities=CAPS)
    assert any("completion condition" in q.lower() for q in cand.unresolved_questions)


def test_observed_vs_inferred_is_preserved():
    """Real browser/network actions are OBSERVED; pipeline-supplied rules are INFERRED (§7)."""
    cand = discover_workflow(_lead_session(), capabilities=CAPS)
    provs = {s.provenance for s in cand.steps}
    assert RuleProvenance.OBSERVED in provs and RuleProvenance.INFERRED in provs


def test_discovery_is_deterministic():
    s = _lead_session()
    a, b = discover_workflow(s, capabilities=CAPS), discover_workflow(s, capabilities=CAPS)
    key = lambda c: [(x.intent, x.provenance.value, x.human_gate.value) for x in c.steps]
    assert key(a) == key(b)
    assert a.unresolved_questions == b.unresolved_questions


def test_no_raw_secret_is_stored_in_observations():
    """§19 — observations reference captured values, they never carry a raw value field."""
    o = _obs(1, K.INPUT, "login", value_ref="cred://vault/x#ref")
    assert not hasattr(o, "value") and o.value_ref.startswith("cred://")


# ──────────────────────────── bridge to the definition + activation gate (§3/§14) ────────────────────────────

def test_candidate_becomes_a_draft_never_active():
    """A demonstration is not executable policy: the candidate bridges to a DRAFT/NEEDS_CLARIFICATION
    definition (never ACTIVE), and the existing activation gate blocks while unknowns remain."""
    cand = discover_workflow(_lead_session(), capabilities=CAPS)
    wf = candidate_to_definition(cand, title="Inbound lead follow-up")
    assert wf.status is WorkflowStatus.NEEDS_CLARIFICATION      # it has unresolved questions
    assert wf.version == 1 and wf.rules
    assert wf.learned_from["recording_refs"] == [cand.session_id]
    with pytest.raises(ValueError):
        activate(wf, actor="owner")                            # unknowns block activation (§14)


def test_clean_candidate_bridges_to_activatable_draft():
    """With every question resolved (all capabilities matched + a completion RESULT), the definition is a
    plain DRAFT that can be activated once reviewed."""
    caps = dict(CAPS)
    cand = discover_workflow(_lead_session(), capabilities=caps)
    # simulate clarification: clear the questions a reviewer answered
    from dataclasses import replace
    resolved = replace(cand, unresolved_questions=())
    wf = candidate_to_definition(resolved)
    assert wf.status is WorkflowStatus.DRAFT
    assert activate(wf, actor="owner").status is WorkflowStatus.ACTIVE
