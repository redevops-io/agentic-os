"""Revenue Missions — the P1 headline demo, offline and end to end.

    "The owner is on a job. The AI answers a lead. The owner approves the response from WhatsApp in
     one tap. Everything else updates automatically."

Run:  python -m agentic_os.revenue.demo
"""
from __future__ import annotations

from .contracts import Disposition, OpportunityType, Priority, RevenueOpportunity
from .gateway import RecordingChannel
from .policy import AutoFollowupPolicy
from .runner import RevenueMissionRun


def main() -> int:
    # 1. An AI voice service answered a lead while the owner was on a job; Discovery normalized it.
    opp = RevenueOpportunity(
        opportunity_id="opp-hvac-1", type=OpportunityType.INBOUND_LEAD, source="ai_voice",
        summary="Called about emergency HVAC service for a 12-unit property. Needs service today.",
        contact_name="Sarah", company="ABC Property Management", requested_service="emergency HVAC",
        channel="whatsapp", estimated_value=8000, urgency="emergency", priority=Priority.P0)

    # 2. Open the Revenue Mission — it runs capture → CRM → draft, then parks at the customer-contact gate.
    phone = RecordingChannel(name="whatsapp")
    run = RevenueMissionRun(opp, owner="Alex (owner)", channel=phone)
    print(f"Mission {run.mission_id} · state after run-to-gate: {run.state.value}")
    print(f"CRM: {opp.crm_record} · draft prepared on {opp.channel}\n")

    # 3. Custom/high-value contact is never auto-sent → the owner gets a one-tap brief on WhatsApp.
    policy = AutoFollowupPolicy(owner="Alex", auto_send_classes=frozenset({"missed_call_reply"}))
    res = run.resolve(policy, message_class="custom_pricing")
    print("── WhatsApp to the owner ──")
    print(phone.sent[-1])
    print(f"\n(authorized as: {res.mode.value} — {res.reason})\n")

    # 4. Owner taps Approve & Send → the send fires and the rest of the mission completes automatically.
    print("Owner taps [Approve & Send] …")
    run.owner("APPROVE_AND_SEND")
    result = run.finalize()
    print(f"Mission state: {result.mission_state.value}")
    print(f"World outcomes: {sorted(result.world)}")
    print(f"Opportunity: owner={opp.owner!r} · stage={opp.stage!r} · "
          f"next='{opp.next_action}' · waiting='{opp.waiting_condition}'")
    print(f"Invariant (owned + a valid next state): {'OK' if result.invariant_ok else 'FAIL'}")

    # 5. Later, a payment is observed → the mission closes WON.
    result = run.close(Disposition.WON)
    print(f"\nPayment observed → disposition: {result.opportunity.disposition.value}")

    print("\nThe owner interacted with one WhatsApp notification; the agent coordinated the systems.")
    return 0 if result.invariant_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
