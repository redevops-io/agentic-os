"""Tests for the immutable, event-sourced InterventionRecord + stores (agentic_os.intervention_record)."""
from __future__ import annotations

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.intervention_record import (
    FileInterventionStore, InMemoryInterventionStore, InterventionRecord, OutcomeLink,
    record_from_selection)
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate, select_action


def _selection():
    opp = DecisionOpportunity(
        entity="Acme", source_app="crm", opportunity_id="crm:Acme",
        candidate_actions=(
            InterventionCandidate("crm", "Acme", "send proposal", 0.8, 0.85, action_kind="send_proposal",
                                  risk_tier=RiskTier.CONSEQUENTIAL, candidate_id="crm:Acme:send_proposal"),
            InterventionCandidate("crm", "Acme", "schedule call", 0.5, 0.8, action_kind="schedule_call",
                                  risk_tier=RiskTier.CONSEQUENTIAL, candidate_id="crm:Acme:schedule_call")))
    return select_action(opp)


def test_record_from_selection_captures_the_decision_context():
    sel = _selection()
    rec = record_from_selection(sel, intervention_id="iv1", policy_version="p-2026.09",
                                proposed_at=100.0, evidence_refs=("obs:1", "obs:2"),
                                executed_at=120.0, execution_ref="msg-42")
    assert rec.intervention_id == "iv1" and rec.opportunity_id == "crm:Acme"
    assert rec.selected_action == "send_proposal"
    assert rec.alternatives and rec.evidence_refs == ("obs:1", "obs:2")
    assert rec.score == sel.expected_utility and rec.policy_version == "p-2026.09"
    assert rec.executed_at == 120.0 and rec.execution_ref == "msg-42"


def test_correlation_is_a_separate_event_records_stay_immutable():
    store = InMemoryInterventionStore()
    rec = record_from_selection(_selection(), intervention_id="iv1", policy_version="p1", proposed_at=1.0)
    store.append(rec)
    store.link(OutcomeLink(intervention_id="iv1", outcome_ref="oc-9", attribution_confidence=0.8, linked_at=200.0))
    store.link(OutcomeLink(intervention_id="iv1", outcome_ref="oc-10", attribution_confidence=0.4, linked_at=260.0))
    # the record object was never mutated to hold outcomes; the join projects them
    assert store.all()[0] == rec
    assert store.outcome_refs("iv1") == ["oc-9", "oc-10"]
    assert store.outcome_refs("nope") == []


def test_file_store_is_durable_and_interleaves_records_and_links(tmp_path):
    path = str(tmp_path / "iv.jsonl")
    s = FileInterventionStore(path)
    s.append(record_from_selection(_selection(), intervention_id="iv1", policy_version="p1", proposed_at=1.0))
    s.link(OutcomeLink("iv1", "oc-1", 0.9, 300.0))
    # a fresh handle (simulated restart) reconstructs both streams from the event log
    s2 = FileInterventionStore(path)
    assert len(s2.all()) == 1 and s2.all()[0].intervention_id == "iv1"
    assert s2.outcome_refs("iv1") == ["oc-1"]


def test_file_store_skips_a_corrupt_tail_line(tmp_path):
    path = str(tmp_path / "iv.jsonl")
    s = FileInterventionStore(path)
    s.append(record_from_selection(_selection(), intervention_id="iv1", policy_version="p1", proposed_at=1.0))
    with open(path, "a", encoding="utf-8") as f:
        f.write("{ partial crash line\n")
    assert len(s.all()) == 1                                        # good record survives
