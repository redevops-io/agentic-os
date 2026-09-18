"""Revenue Missions P0 — synthetic capture → owner-approval → send → disposition replay, over the
real Mission Runtime (compile / schedule / execute / park / approve / events)."""
from __future__ import annotations

from agentic_os.mission.executor import Executor
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState
from agentic_os.revenue import (
    AutoFollowupPolicy, Disposition, OpportunityType, Priority, RecordingChannel,
    RevenueOpportunity, RevenueMissionRun, SendMode, next_action_invariant,
)
from agentic_os.revenue.fleet import revenue_fleet


def _lead(**kw) -> RevenueOpportunity:
    base = dict(
        opportunity_id="opp-1", type=OpportunityType.INBOUND_LEAD, source="ai_voice",
        summary="Called about emergency HVAC for a 12-unit property. Needs service today.",
        contact_name="Sarah", company="ABC Property Management", requested_service="emergency HVAC",
        channel="whatsapp", estimated_value=8000, urgency="emergency", priority=Priority.P0)
    base.update(kw)
    return RevenueOpportunity(**base)


def test_approve_and_send_end_to_end():
    ch = RecordingChannel()
    opp = _lead()
    run = RevenueMissionRun(opp, owner="Alex", channel=ch)

    # the mission ran to the customer-contact gate and parked for the owner
    assert run.state is MissionState.WAITING_HUMAN
    assert run.opp.crm_record.startswith("twenty:opp:")        # CRM upsert ran pre-gate
    assert run.opp.proposed_response                            # a draft was prepared pre-gate

    # a message class that is never pre-authorized → owner-in-the-loop; brief delivered to the channel
    policy = AutoFollowupPolicy(owner="Alex")                   # nothing pre-authorized
    res = run.resolve(policy, "custom_pricing")
    assert res.mode is SendMode.APPROVE_AND_SEND and not res.resolved
    assert ch.sent and "Approve & Send" in ch.sent[-1] and "Sarah" in ch.sent[-1]

    # owner taps Approve & Send → mission completes end to end
    run.owner("APPROVE_AND_SEND")
    assert run.state is MissionState.SUCCEEDED
    result = run.finalize()
    assert {"opportunity_recorded", "response_drafted", "followup_sent",
            "activity_logged", "next_action_scheduled"} <= set(result.world)
    assert result.invariant_ok                                  # owner + a scheduled next action / waiting
    assert result.opportunity.waiting_condition


def test_policy_auto_send_no_human():
    ch = RecordingChannel()
    opp = _lead(type=OpportunityType.MISSED_CALL, summary="Missed call from a returning customer.")
    run = RevenueMissionRun(opp, owner="Alex", channel=ch)
    assert run.state is MissionState.WAITING_HUMAN

    # owner pre-authorized missed-call replies → auto-send, no human tap
    policy = AutoFollowupPolicy(owner="Alex", auto_send_classes=frozenset({"missed_call_reply"}))
    res = run.resolve(policy, "missed_call_reply", existing_relationship=True)
    assert res.mode is SendMode.POLICY_AUTO_SEND and res.resolved and res.by == "policy"
    assert run.state is MissionState.SUCCEEDED
    assert ch.sent and "[auto-sent per your policy]" in ch.sent[-1]
    assert run.finalize().invariant_ok


def test_never_auto_class_forces_owner_approval_even_if_listed():
    opp = _lead()
    run = RevenueMissionRun(opp, owner="Alex")
    # even if the owner mistakenly lists a gov bid submission as auto, the safety floor forces approval
    policy = AutoFollowupPolicy(owner="Alex", auto_send_classes=frozenset({"gov_bid_submission"}))
    res = run.resolve(policy, "gov_bid_submission")
    assert res.mode is SendMode.APPROVE_AND_SEND and not res.resolved
    assert run.state is MissionState.WAITING_HUMAN


def test_reject_keeps_the_lead_owned():
    opp = _lead()
    run = RevenueMissionRun(opp, owner="Alex")
    run.resolve(AutoFollowupPolicy(owner="Alex"), "custom_pricing")
    note = run.owner("REJECT")
    assert "kept" in note
    assert run.state is MissionState.FAILED
    result = run.finalize()
    # a rejected auto-send must NOT lose the lead — still owned, still has a next action
    assert result.invariant_ok and result.opportunity.owner == "Alex" and result.opportunity.next_action


def test_close_won_on_payment_observed():
    opp = _lead()
    run = RevenueMissionRun(opp, owner="Alex")
    run.resolve(AutoFollowupPolicy(owner="Alex"), "custom_pricing")
    run.owner("APPROVE_AND_SEND")
    run.finalize()
    result = run.close(Disposition.WON)                        # payment/booking observed later
    assert result.opportunity.disposition is Disposition.WON and result.invariant_ok


def test_restart_resume_from_event_log(tmp_path):
    opp = _lead()
    store = EventStore(path=str(tmp_path / "rev.jsonl"))
    run = RevenueMissionRun(opp, owner="Alex", store=store)
    assert run.state is MissionState.WAITING_HUMAN
    node_id = run.rt.repo.pending_human(run.mission_id)["node_id"]

    # crash + restart: fresh store from disk + fresh runtime, then approve
    reg, client = revenue_fleet(opp)
    rt2 = MissionRuntime(reg, Executor(client), store=EventStore(path=str(tmp_path / "rev.jsonl")))
    rt2.rehydrate(run.mission_id)
    rt2.approve(run.mission_id, node_id, "approve")
    assert rt2._missions[run.mission_id].state is MissionState.SUCCEEDED


def test_invariant_semantics():
    o = _lead()
    o.owner = ""
    assert not next_action_invariant(o)                        # no owner
    o.owner = "Alex"
    assert not next_action_invariant(o)                        # owner but no next state
    o.next_action = "call back"
    assert next_action_invariant(o)
    o.next_action = ""
    o.disposition = Disposition.WON
    assert next_action_invariant(o)                            # terminal disposition is a valid end


# ── collector layer: sources → dedup → opportunities → governed missions ──
def test_collector_dedup_and_open_missions():
    from agentic_os.revenue import (
        InMemorySource, RevenueSignal, SamGovSource, collect, open_missions,
    )

    sig = RevenueSignal(source="ai_voice", external_id="call-9", type=OpportunityType.INBOUND_LEAD,
                        summary="Emergency HVAC lead", contact_name="Sarah", channel="whatsapp",
                        priority=Priority.P0)
    dup = RevenueSignal(source="ai_voice", external_id="call-9", type=OpportunityType.INBOUND_LEAD,
                        summary="Emergency HVAC lead (again)")           # same source id → deduped
    other = RevenueSignal(source="web_form", external_id="wf-3", type=OpportunityType.INBOUND_LEAD,
                          summary="Quote request", company="ACME")
    src = InMemorySource("mixed", [sig, dup, other])

    opps = collect([src, SamGovSource()])                               # SAM.gov yields nothing offline
    assert len(opps) == 2                                                # duplicate collapsed
    ids = {o.opportunity_id for o in opps}
    assert ids == {"ai_voice:call-9", "web_form:wf-3"}

    runs = open_missions(opps, owner="Alex", channel_factory=RecordingChannel)
    assert len(runs) == 2
    for run in runs:                                                     # each parked at its send gate
        assert run.state is MissionState.WAITING_HUMAN
        run.resolve(AutoFollowupPolicy(owner="Alex"), "inquiry_acknowledgement")
        run.owner("APPROVE_AND_SEND")
        assert run.finalize().invariant_ok


def test_samgov_source_offline_is_empty_but_enabled_with_key():
    from agentic_os.revenue import SamGovSource
    assert SamGovSource().poll() == []                                  # no key → framework still runs
    assert SamGovSource(api_key="x").enabled is True
