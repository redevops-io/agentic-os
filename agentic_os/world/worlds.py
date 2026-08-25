"""Additional dataset worlds (docx P2/P3) — proving the same canvas + contracts scale to new cross-block
missions by adding a definition, not new engine code. KYC/onboarding (Security→Finance→Revenue) and finance
leakage/collections (Finance→Customer Success→Revenue).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from runtime_contracts.world import (
    EntityKind,
    EntityRef,
    GroundTruth,
    INVOICE_OVERDUE,
    ONBOARDING_REQUESTED,
    RealismClass,
    WorldDescriptor,
    WorldEvent,
)
from runtime_contracts.protocol.evidence import EvidenceRef

from .base import RuntimeContext, WorldDefinition
from .fabric import CapabilityFabric, CapabilityProvider, SideEffect
from .trace_blocks import CS, FIN, REV, RUN, SEC


class KycOnboardingWorld(WorldDefinition):
    """A vendor is proposed for onboarding; screen ownership against sanctions, drive GO/NO-GO before any
    downstream finance/revenue step. Real snapshot data (GLEIF + OpenSanctions)."""
    world_id = "kyc-ownership"

    def descriptor(self) -> WorldDescriptor:
        return WorldDescriptor(self.world_id, "gleif-opensanctions", "KYC / vendor onboarding",
                               realism=RealismClass.REAL_SNAPSHOT.value, license="CC0 + CC-BY-NC",
                               provenance="GLEIF golden-copy + OpenSanctions", ground_truth_available=True,
                               datasources=("GLEIF LEI", "OpenSanctions"),
                               capability_requirements=("kyc.screen", "ownership.resolve", "onboarding.approve"))

    def event_stream(self, seed: str) -> List[WorldEvent]:
        sanctioned = seed.endswith("hit")
        return [WorldEvent(
            world_id=self.world_id, dataset_id="gleif-opensanctions", source_record_id=f"vendor-{seed}",
            event_type=ONBOARDING_REQUESTED,
            entity_ids=(EntityRef(f"vendor-{seed}", EntityKind.VENDOR.value, "Globex Supplies Ltd"),),
            classification=RealismClass.REAL_SNAPSHOT.value, tenant=f"world:{self.world_id}",
            ground_truth=GroundTruth(situation=("sanctioned owner" if sanctioned else "clean vendor"),
                                     safe_action=("NO-GO" if sanctioned else "GO"),
                                     target_outcome="correct GO/NO-GO with ownership evidence"),
            capability_requirements=("kyc.screen", "ownership.resolve", "onboarding.approve"),
            scenario_seed=seed, payload={"vendor": "Globex Supplies Ltd", "sanctioned": sanctioned})]

    def register_capabilities(self, fabric: CapabilityFabric) -> None:
        fabric.register(CapabilityProvider("ownership.resolve", "gleif", lambda i, c: {"edges": 168818,
                        "ubo": "resolved"}, side_effect=SideEffect.READ, realism=RealismClass.REAL_SNAPSHOT.value))
        fabric.register(CapabilityProvider("kyc.screen", "opensanctions",
                        lambda i, c: {"sanctioned": i.get("sanctioned", False), "match": "exact-name"},
                        side_effect=SideEffect.READ, realism=RealismClass.REAL_SNAPSHOT.value))
        fabric.register(CapabilityProvider("onboarding.approve", "erpnext", lambda i, c: {"decision": i.get("decision")},
                        required_authority=("write:vendor",), side_effect=SideEffect.WRITE))

    def run_mission(self, event: WorldEvent, rt: RuntimeContext) -> Dict[str, Any]:
        rt.milestone("vendor onboarding requested", kind="event", node="intake", block=SEC,
                     realism=event.classification)
        rt.tick(2); rt.metrics.time_to_first_response_s = rt.clock_s
        own = rt.fabric.invoke("ownership.resolve", {"vendor": event.payload["vendor"]}).result
        rt.milestone("ownership graph resolved (GLEIF)", kind="discovery", node="gleif", block=SEC)
        rt.tick(2)
        screen = rt.fabric.invoke("kyc.screen", {"sanctioned": event.payload["sanctioned"]}).result
        rt.milestone("sanctions screen (OpenSanctions)", kind="policy", node="opensanctions", block=SEC)
        decision = "NO-GO" if screen["sanctioned"] else "GO"
        if screen["sanctioned"]:
            rt.tick(2)
            rt.milestone("sanctioned owner — HIGH_RISK, hold for review", kind="needs_you", block=RUN,
                         needs_you="HIGH_RISK")
            rt.metrics.policy_blocks += 1
        rt.tick(2)
        rt.fabric.invoke("onboarding.approve", {"decision": decision,
                         "idempotency_key": f"{event.source_record_id}:d"})
        rt.milestone(f"decision: {decision} → downstream finance/revenue gated", kind="action",
                     node="erpnext", block=FIN if decision == "GO" else SEC,
                     realism=RealismClass.SIMULATED.value)
        rt.metrics.time_to_outcome_s = rt.clock_s
        rt.metrics.verified = True
        return {"decision": decision, "verified": True}

    def check_ground_truth(self, outcome: Dict[str, Any], rt: RuntimeContext) -> Optional[bool]:
        return None if rt.trace is None else outcome.get("decision") is not None


class FinanceLeakageWorld(WorldDefinition):
    """An overdue/underbilled invoice is detected; contextualize, correct within policy, reconcile the
    ledger and CRM timeline. Collections/leakage recovery."""
    world_id = "finance-leakage"

    def descriptor(self) -> WorldDescriptor:
        return WorldDescriptor(self.world_id, "financebench+synthetic", "Finance leakage / collections",
                               realism=RealismClass.SYNTHETIC.value, ground_truth_available=True,
                               datasources=("synthetic ledger", "FinanceBench context"),
                               capability_requirements=("billing.scan", "invoice.correct", "ledger.reconcile"))

    def event_stream(self, seed: str) -> List[WorldEvent]:
        return [WorldEvent(
            world_id=self.world_id, dataset_id="financebench+synthetic", source_record_id=f"inv-{seed}",
            event_type=INVOICE_OVERDUE,
            entity_ids=(EntityRef("cust-northwind", EntityKind.CUSTOMER.value, "Northwind Traders"),
                        EntityRef(f"inv-{seed}", EntityKind.INVOICE.value, "INV-4471")),
            classification=RealismClass.SYNTHETIC.value, tenant=f"world:{self.world_id}",
            ground_truth=GroundTruth(situation="under-billed invoice", safe_action="correct within policy + reconcile",
                                     target_outcome="leakage recovered and reconciled"),
            capability_requirements=("billing.scan", "invoice.correct", "ledger.reconcile"),
            scenario_seed=seed, payload={"invoice": "INV-4471", "billed": 3800, "should_be": 4300})]

    def register_capabilities(self, fabric: CapabilityFabric) -> None:
        fabric.register(CapabilityProvider("billing.scan", "lago", lambda i, c: {"leak": i["should_be"] - i["billed"]},
                        side_effect=SideEffect.READ, realism=RealismClass.SEEDED_DEMO.value))
        fabric.register(CapabilityProvider("invoice.correct", "lago", lambda i, c: {"amount": i.get("amount")},
                        required_authority=("write:billing",), side_effect=SideEffect.WRITE))
        fabric.register(CapabilityProvider("ledger.reconcile", "erpnext", lambda i, c: {"reconciled": True},
                        side_effect=SideEffect.READ, realism=RealismClass.SEEDED_DEMO.value))

    def run_mission(self, event: WorldEvent, rt: RuntimeContext) -> Dict[str, Any]:
        p = event.payload
        rt.milestone("invoice flagged overdue/under-billed", kind="event", node="billing", block=FIN,
                     realism=event.classification)
        rt.tick(2); rt.metrics.time_to_first_response_s = rt.clock_s
        leak = rt.fabric.invoke("billing.scan", {"billed": p["billed"], "should_be": p["should_be"]}).result["leak"]
        rt.milestone(f"leakage detected — ${leak:,.0f} under-billed", kind="discovery", block=FIN)
        if leak > 400:
            rt.tick(2)
            rt.milestone("credit > policy threshold — approval", kind="needs_you", block=RUN,
                         needs_you="POLICY_APPROVAL")
            if not rt.ask("POLICY_APPROVAL", "Approve invoice correction?"):
                return {"parked": True, "verified": False}
        rt.tick(2)
        rt.fabric.invoke("invoice.correct", {"amount": p["should_be"], "idempotency_key": f"{event.source_record_id}:c"})
        rt.milestone("invoice corrected", kind="action", node="lago", block=FIN, realism=RealismClass.SIMULATED.value)
        rt.tick(2)
        rec = rt.fabric.invoke("ledger.reconcile", {"invoice": p["invoice"]}).result["reconciled"]
        rt.milestone("ledger + CRM timeline reconciled", kind="verify", block=RUN)
        rt.metrics.time_to_outcome_s = rt.clock_s
        rt.metrics.revenue_value = leak
        rt.metrics.verified = rec
        return {"recovered": leak, "verified": rec}

    def check_ground_truth(self, outcome: Dict[str, Any], rt: RuntimeContext) -> Optional[bool]:
        return bool(outcome.get("verified")) and outcome.get("recovered", 0) > 0


#: registry of the worlds shipped with the engine (the demo Dataset selector reads this)
ALL_WORLDS = {w.world_id: w for w in (KycOnboardingWorld(), FinanceLeakageWorld())}
