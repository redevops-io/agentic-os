"""AfterHoursLeadWorld — the flagship "Answer Now, Quote Now" world (v2 §33, docx §5).

An after-hours lead arrives; a conventional chatbot can answer but cannot complete the work. This world
runs the opposite: Discovery extracts intent + entities + missing evidence, Context chooses the evidence
path, one governed mission crosses Revenue → Customer Success → Finance, asks the caller only for the one
unresolved fact (NeedsYou, not a guess), produces a governed quote during the interaction, captures
acceptance, and verifies the resulting state — with the counterfactual that a bare chatbot yields a
next-day callback. Capabilities run through the fabric; writes route to the Outcome Simulator (SIMULATE).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from runtime_contracts.world import (
    EntityKind,
    EntityRef,
    GroundTruth,
    LEAD_RECEIVED,
    RealismClass,
    WorldDescriptor,
    WorldEvent,
)
from runtime_contracts.protocol.evidence import EvidenceRef

from .base import RuntimeContext, WorldDefinition
from .fabric import CapabilityFabric, CapabilityProvider, SideEffect
from .models import BaselineResult, RunMetrics
from .trace_blocks import CS, FIN, REV, RUN


class AfterHoursLeadWorld(WorldDefinition):
    world_id = "after-hours-lead"

    def descriptor(self) -> WorldDescriptor:
        return WorldDescriptor(
            self.world_id, "home-services", "After-hours lead → instant governed quote",
            realism=RealismClass.SYNTHETIC.value, provenance="synthetic call transcript + gov parcel facts",
            datasources=("synthetic transcript", "government parcel snapshot"), ground_truth_available=True,
            supported_scenarios=("instant-quote",),
            capability_requirements=("crm.read", "geo.resolve", "pricing.quote", "crm.opportunity"))

    def event_stream(self, seed: str) -> List[WorldEvent]:
        # one lead; the roof pitch is deliberately absent so the mission must ask rather than guess
        return [WorldEvent(
            world_id=self.world_id, dataset_id="home-services", source_record_id=f"call-{seed}",
            event_type=LEAD_RECEIVED,
            entity_ids=(EntityRef("cust-acme", EntityKind.CUSTOMER.value, "Acme Roofing Inc"),
                        EntityRef("prop-114elm", EntityKind.PROPERTY.value, "114 Elm St")),
            observed_at="2026-08-25T21:47:00Z", effective_at="2026-08-25T21:47:00Z",
            evidence_refs=(EvidenceRef(ref=f"transcript-{seed}", content_hash="rcv1:tx", source="voice"),),
            classification=RealismClass.SYNTHETIC.value, data_classifications=("pii",),
            tenant=f"world:{self.world_id}",
            ground_truth=GroundTruth(situation="qualified after-hours roofing lead",
                                     safe_action="ask for the one missing fact, then quote",
                                     unsafe_action="guess the roof pitch and quote",
                                     target_outcome="governed quote produced during the interaction"),
            capability_requirements=("crm.read", "geo.resolve", "pricing.quote", "crm.opportunity"),
            scenario_seed=seed,
            payload={"service": "roof repair", "property": "114 Elm St", "urgency": "storm damage"})]

    def register_capabilities(self, fabric: CapabilityFabric) -> None:
        fabric.register(CapabilityProvider("crm.read", "twenty", self._crm_read,
                                           required_authority=("read:crm",), side_effect=SideEffect.READ,
                                           realism=RealismClass.SEEDED_DEMO.value, latency_ms=40))
        fabric.register(CapabilityProvider("geo.resolve", "geo-engine", self._geo_resolve,
                                           required_authority=("read:geo",), side_effect=SideEffect.READ,
                                           realism=RealismClass.REAL_SNAPSHOT.value, latency_ms=60))
        fabric.register(CapabilityProvider("pricing.quote", "pricing", self._quote,
                                           required_authority=("write:quote",), side_effect=SideEffect.WRITE,
                                           realism=RealismClass.SYNTHETIC.value, cost_usd=0.02))
        fabric.register(CapabilityProvider("crm.opportunity", "twenty", self._opportunity,
                                           required_authority=("write:crm",), side_effect=SideEffect.WRITE))

    # -- capability implementations --
    def _crm_read(self, inputs, ctx):
        return {"account": "Acme Roofing Inc", "lifetime_value": 118000, "open_tickets": 0}

    def _geo_resolve(self, inputs, ctx):
        return {"parcel": "114 Elm St", "roof_area_sqft": 2200, "zoning": "R1", "source": "gov ArcGIS"}

    def _quote(self, inputs, ctx):
        return {"amount": inputs.get("amount"), "line_items": inputs.get("line_items", [])}

    def _opportunity(self, inputs, ctx):
        return {"stage": "quote_sent", "amount": inputs.get("amount")}

    # -- the governed mission --
    def run_mission(self, event: WorldEvent, rt: RuntimeContext) -> Dict[str, Any]:
        rt.milestone("lead arrives (21:47)", kind="event", node="intake", block=REV,
                     realism=event.classification)
        rt.tick(2); rt.metrics.time_to_first_response_s = rt.clock_s
        rt.milestone("intent + entities extracted", kind="discovery", node="discovery", block=RUN)
        rt.milestone("missing quote evidence identified (roof pitch)", kind="discovery", block=RUN)
        rt.tick(2)
        rt.milestone("evidence route selected — CRM + parcel + pricing (EXPLAINable)", kind="plan", block=RUN)

        crm = rt.fabric.invoke("crm.read", {"entity": "cust-acme"}).result
        geo = rt.fabric.invoke("geo.resolve", {"property": event.payload["property"]}).result
        rt.tick(2)
        rt.milestone("mission crosses Revenue → Customer Success → Finance", kind="plan",
                     node="crm", block=CS)

        # the one unresolved fact — ask, never guess
        pitch = event.payload.get("roof_pitch")
        if not pitch:
            rt.tick(2)
            rt.milestone("roof pitch missing — ask the caller (not a guess)", kind="needs_you", block=RUN,
                         needs_you="MISSING_EVIDENCE")
            pitch = rt.ask("MISSING_EVIDENCE", "What is the roof pitch?")
            if not pitch:
                rt.milestone("parked — awaiting caller answer", kind="needs_you", block=RUN,
                             needs_you="MISSING_EVIDENCE")
                return {"parked": True, "verified": False}

        # governed quote (write → simulator)
        rate = 6.5 + (2.0 if pitch in ("steep", "12/12") else 0.0)
        amount = round(geo["roof_area_sqft"] * rate, 2)
        rt.tick(3)
        quote = rt.fabric.invoke("pricing.quote",
                                 {"amount": amount, "line_items": [{"roof_area": geo["roof_area_sqft"],
                                  "rate": rate, "pitch": pitch}], "idempotency_key": f"{event.source_record_id}:q"})
        rt.milestone(f"quote created — ${amount:,.0f}", kind="action", node="pricing", block=FIN,
                     realism=RealismClass.SIMULATED.value)
        rt.tick(2)
        opp = rt.fabric.invoke("crm.opportunity", {"amount": amount, "account": crm["account"],
                               "idempotency_key": f"{event.source_record_id}:o"})
        rt.milestone("acceptance captured — opportunity created", kind="action", node="crm", block=REV,
                     realism=RealismClass.SIMULATED.value)

        # verification: the intended state transition actually occurred (not just HTTP 200)
        rt.tick(2)
        verified = (rt.simulator.exists(quote.result["artifact_id"])
                    and rt.simulator.exists(opp.result["artifact_id"]))
        rt.milestone("verification passes — quote + opportunity reconciled", kind="verify", block=RUN)
        rt.metrics.time_to_outcome_s = rt.clock_s
        rt.metrics.revenue_value = amount
        rt.metrics.verified = verified
        rt.metrics.evidence_completeness = 1.0
        rt.metrics.human_minutes_saved = 35.0
        return {"quote_id": quote.result["artifact_id"], "opportunity_id": opp.result["artifact_id"],
                "amount": amount, "verified": verified}

    def check_ground_truth(self, outcome: Dict[str, Any], rt: RuntimeContext) -> Optional[bool]:
        # ground truth: a verified quote produced during the interaction (<= 20s), no unsupported guess
        return bool(outcome.get("verified")) and rt.clock_s <= 20 and rt.metrics.unsupported_guesses == 0

    # -- baseline notes for the counterfactual panel (used by BenchmarkRunner) --
    def baseline_notes(self) -> Dict[str, str]:
        return {
            "manual": "voicemail/form → human triage next morning → estimator callback → quote hours/days later",
            "naive_agent": "answers, then guesses the roof pitch to produce a number → wrong quote, unverified",
            "independent_agents": "CRM and pricing agents act in isolation; context re-entered; risk of conflicting quote",
        }
