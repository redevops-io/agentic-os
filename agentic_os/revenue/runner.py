"""Run a Revenue Mission end-to-end over the real Mission Runtime.

Ties the pieces to the existing kernel: a `RevenueOpportunity` opens an `inbound_lead` mission, the
mission runs to the customer-contact gate (`WAITING_HUMAN`), the `OwnerAttentionGateway` resolves that
gate (owner tap or pre-authorized policy), the mission completes, and the opportunity's lifecycle
fields are written back — enforcing the core invariant. No new runtime; the Mission Runtime owns
lifecycle, approvals, saga, events and replay.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..mission.executor import Executor
from ..mission.runtime import MissionRuntime
from ..mission.store import EventStore
from ..mission.types import MissionState
from .contracts import Disposition, RevenueOpportunity, assert_invariant, next_action_invariant
from .fleet import revenue_fleet
from .gateway import AttentionChannel, OwnerAttentionGateway, SendResolution
from .policy import AutoFollowupPolicy

_POLICY_REFS = ["crm:write", "revenue:draft", "revenue:send", "revenue:schedule"]


@dataclass
class RevenueMissionResult:
    mission_state: MissionState
    opportunity: RevenueOpportunity
    world: dict
    timeline: list
    invariant_ok: bool


class RevenueMissionRun:
    """A live Revenue Mission: opened, parked at the send gate, awaiting resolution."""

    def __init__(self, opp: RevenueOpportunity, *, owner: str,
                 channel: Optional[AttentionChannel] = None, store: Optional[EventStore] = None):
        opp.owner = owner or opp.owner
        self.opp = opp
        reg, client = revenue_fleet(opp)
        self.rt = MissionRuntime(reg, Executor(client), store=store or EventStore())
        self.gateway = OwnerAttentionGateway(channel)
        m = self.rt.create_mission(goal=opp.summary, policy_refs=_POLICY_REFS, template="inbound_lead")
        self.mission_id = m.id
        opp.mission_id = m.id
        self.rt.run(m.id)                    # runs to the customer-contact gate → WAITING_HUMAN
        self._absorb_draft()

    def _world(self) -> dict:
        return self.rt._world(self.mission_id).snapshot()

    def _absorb_draft(self) -> None:
        """Pull the CRM id + drafted response the pre-gate steps produced onto the opportunity, so the
        owner's brief and the sent message are real."""
        w = self._world()
        rec = w.get("opportunity_recorded")
        if rec and getattr(rec, "value", None):
            self.opp.crm_record = rec.value.get("crm_record", self.opp.crm_record)
            self.opp.stage = rec.value.get("stage", self.opp.stage)
        drafted = w.get("response_drafted")
        if drafted and getattr(drafted, "value", None) and not self.opp.proposed_response:
            self.opp.proposed_response = drafted.value.get("draft", "")
            self.opp.channel = self.opp.channel or drafted.value.get("channel", "")

    @property
    def state(self) -> MissionState:
        return self.rt._missions[self.mission_id].state

    def resolve(self, policy: AutoFollowupPolicy, message_class: str, **guards) -> SendResolution:
        """Apply the auto-follow-up policy to the parked send gate (auto-approve if pre-authorized)."""
        return self.gateway.request_send(self.rt, self.mission_id, self.opp,
                                         policy=policy, message_class=message_class, **guards)

    def owner(self, action: str) -> str:
        """Apply the owner's one-tap decision to the parked send gate."""
        return self.gateway.owner_action(self.rt, self.mission_id, action, self.opp)

    def finalize(self) -> RevenueMissionResult:
        """Write the mission outcome back onto the opportunity and enforce the invariant."""
        w = self._world()
        if self.state is MissionState.SUCCEEDED:
            sched = w.get("next_action_scheduled")
            if sched and getattr(sched, "value", None):
                self.opp.next_action = sched.value.get("next_action", self.opp.next_action)
                self.opp.waiting_condition = sched.value.get("waiting_condition", self.opp.waiting_condition)
            self.opp.stage = "contacted"
        elif self.opp.disposition is Disposition.OPEN and not (self.opp.next_action or self.opp.waiting_condition):
            # a failed/rejected send must still leave the lead owned with a next action
            self.opp.next_action = "manual follow-up (automated send did not complete)"
        assert_invariant(self.opp)
        return RevenueMissionResult(
            mission_state=self.state, opportunity=self.opp, world=w,
            timeline=self.rt.repo.timeline(self.mission_id), invariant_ok=next_action_invariant(self.opp))

    def close(self, disposition: Disposition) -> RevenueMissionResult:
        """Terminal disposition (e.g. payment observed → WON). Clears the open next-action requirement."""
        self.opp.disposition = disposition
        if disposition is not Disposition.OPEN:
            self.opp.next_action = ""
            self.opp.waiting_condition = ""
        assert_invariant(self.opp)
        return RevenueMissionResult(
            mission_state=self.state, opportunity=self.opp, world=self._world(),
            timeline=self.rt.repo.timeline(self.mission_id), invariant_ok=next_action_invariant(self.opp))
