"""Tests for the immutable, event-sourced InterventionRecord + stores (agentic_os.intervention_record)."""
from __future__ import annotations

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.intervention_record import (
    ActorType, FileInterventionStore, InMemoryInterventionStore, InterventionRecord, OutcomeLink,
    record_from_selection, record_human_action, select_and_record, shadow_report)
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


# ── record-before-surface boundary (PR2) ─────────────────────────────────────────────
def _four_action_opp(**vals):
    # the narrow first outreach decision: contact / investigate / wait / do_not_contact
    defaults = {"contact": 0.8, "investigate": 0.5, "wait": 0.3, "do_not_contact": 0.1}
    defaults.update(vals)
    from agentic_os.agent_gateway.contracts import RiskTier
    tiers = {"contact": RiskTier.CONSEQUENTIAL, "investigate": RiskTier.READ,
             "wait": RiskTier.READ, "do_not_contact": RiskTier.READ}
    cands = tuple(InterventionCandidate("outreach", "Prospect", f"{k}", ev, 0.8, action_kind=k,
                                        risk_tier=tiers[k], candidate_id=f"outreach:Prospect:{k}")
                  for k, ev in defaults.items())
    return DecisionOpportunity(entity="Prospect", source_app="outreach",
                               candidate_actions=cands, opportunity_id="outreach:Prospect")


def test_select_and_record_persists_before_surface():
    store = InMemoryInterventionStore()
    sel, rec = select_and_record(_four_action_opp(), store, policy_version="p1", proposed_at=100.0,
                                 id_fn=lambda: "iv-1", evidence_refs=("obs:1",))
    assert store.all() == [rec]                                     # durable before the caller surfaces sel
    assert rec.selected_action == sel.action.action_kind and rec.evidence_refs == ("obs:1",)
    assert rec.intervention_id == "iv-1" and rec.opportunity_id == "outreach:Prospect"


def test_a_wait_or_do_not_contact_recommendation_is_also_durable():
    # even when the runtime chooses NOT to act, the recommendation is recorded (odt: WAIT/DO_NOT_CONTACT
    # must be durable). Force it by making every outbound option net-negative so do-nothing/monitor wins.
    store = InMemoryInterventionStore()
    opp = _four_action_opp(contact=-0.4, investigate=-0.2, wait=0.05, do_not_contact=0.02)
    sel, rec = select_and_record(opp, store, policy_version="p1", proposed_at=100.0, id_fn=lambda: "iv-2")
    assert len(store.all()) == 1                                    # persisted regardless of the choice
    assert rec.intervention_id == "iv-2"
    assert rec.selected_action in ("wait", "do_not_contact", "do nothing")   # a non-contact recommendation


# ── Shadow mode: the human counterfactual (PR3) ──────────────────────────────────────
def test_human_action_is_an_intervention_with_a_human_actor():
    store = InMemoryInterventionStore()
    rec = record_human_action(store, opportunity_id="outreach:Prospect", action_kind="schedule_meeting",
                              at=200.0, intervention_id="h1")
    assert rec.actor_type is ActorType.HUMAN and rec.selected_action == "schedule_meeting"
    assert rec.executed_at == 200.0 and store.all() == [rec]


def test_shadow_report_pairs_runtime_recommendation_with_human_action():
    store = InMemoryInterventionStore()
    # opp A: runtime said contact, human also contacted → agreement
    select_and_record(_four_action_opp(contact=0.9), store, policy_version="p1", proposed_at=10.0,
                      id_fn=lambda: "r-A")            # opportunity_id = outreach:Prospect
    record_human_action(store, opportunity_id="outreach:Prospect", action_kind="contact", at=20.0,
                        intervention_id="h-A")
    # opp B: runtime recommended WAIT, human overrode and contacted → override
    optB = _four_action_opp(contact=-0.5, wait=0.1)
    optB = DecisionOpportunity(entity="B", source_app="outreach",
                               candidate_actions=optB.candidate_actions, opportunity_id="outreach:B")
    select_and_record(optB, store, policy_version="p1", proposed_at=10.0, id_fn=lambda: "r-B")
    record_human_action(store, opportunity_id="outreach:B", action_kind="contact", at=20.0,
                        intervention_id="h-B")

    rep = shadow_report(store)
    assert rep.paired == 2 and rep.agreements == 1 and rep.overrides == 1
    assert rep.agreement_rate == 0.5 and rep.override_rate == 0.5
    by_opp = {p.opportunity_id: p for p in rep.pairs}
    assert by_opp["outreach:Prospect"].agreed is True
    assert by_opp["outreach:B"].agreed is False and by_opp["outreach:B"].human_action == "contact"
