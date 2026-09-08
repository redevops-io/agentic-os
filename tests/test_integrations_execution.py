"""W4 — running a ConnectPlan: connect providers, run the governed test Mission, EXPLAIN.

Uses a local PaperAdapter that satisfies the AdapterPort structurally (exactly as a real
redevops-connectors adapter does), so the whole request → ConnectPlan → connect → run →
explain path is exercised with no live call and no cross-repo dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

import pytest

from agentic_os.integrations import (
    AdapterRegistry,
    CapabilityDimension,
    CapabilityRequirement,
    CapabilityRequirementGraph,
    IntegrationManifest,
    IntegrationProposal,
    KeywordReader,
    PlanNotConnectable,
    Support,
    compile_integration_intent,
    connect_providers,
    explain,
    plan_from_request,
    run_test_mission,
)


# ── a paper adapter shaped like a real redevops-connectors adapter ──────────────
@dataclass(frozen=True)
class _Cap:
    name: str
    tier: int
    write: bool


@dataclass(frozen=True)
class _Result:
    ok: bool
    provider_object_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class _State:
    connected: bool
    detail: str = ""


@dataclass(frozen=True)
class _Obs:
    found: bool


@dataclass
class PaperAdapter:
    provider: str
    caps: Tuple[_Cap, ...]
    _created: set = field(default_factory=set)
    observable: bool = True

    def capabilities(self) -> Tuple[_Cap, ...]:
        return self.caps

    def connect(self, config: Mapping[str, Any], credential_ref: str) -> _State:
        return _State(connected=True, detail=f"{self.provider} connected")

    def execute(self, capability: str, request: Mapping[str, Any], envelope: Optional[object]) -> _Result:
        cap = next((c for c in self.caps if c.name == capability), None)
        if cap is None:
            return _Result(ok=False, error="unsupported")
        if cap.write and envelope is None:
            return _Result(ok=False, error="execution envelope required")
        oid = f"{self.provider}:{capability}:1"
        self._created.add(oid)
        return _Result(ok=True, provider_object_id=oid)

    def observe(self, resource_ref: str) -> _Obs:
        if not self.observable:
            raise RuntimeError("not observable")
        return _Obs(found=resource_ref in self._created)

    def health(self):
        return _State(connected=True)


def _manifest() -> IntegrationManifest:
    return IntegrationManifest(dimensions=(
        CapabilityDimension("chat.message.send", "whatsapp_business", Support.EXECUTED, tier=3),
        CapabilityDimension("crm.contact.upsert", "hubspot", Support.EXECUTED, tier=2),
        CapabilityDimension("billing.refund.execute", "stripe", Support.EXECUTED, tier=4),
        CapabilityDimension("approval.request", "slack", Support.EXECUTED, tier=3),
    ))


def _registry() -> AdapterRegistry:
    return (AdapterRegistry()
            .register(PaperAdapter("whatsapp_business", (_Cap("chat.message.send", 3, True),)))
            .register(PaperAdapter("hubspot", (_Cap("crm.contact.upsert", 2, True),)))
            .register(PaperAdapter("stripe", (_Cap("billing.refund.execute", 4, True),)))
            .register(PaperAdapter("slack", (_Cap("approval.request", 3, True),))))


_REQUEST = "answer whatsapp support, update the crm, refund via stripe, approve in slack"
_ANSWERS = {
    "Who is allowed to approve refunds?": "Finance team",
    "Which account or number should send replies?": "+123",
}


def _plan():
    return plan_from_request(_REQUEST, reader=KeywordReader.default(), manifest=_manifest(),
                             confirmed_by="alex", confirmed_at="2026-09-08T00:00:00Z", answers=_ANSWERS)


# ── connect ─────────────────────────────────────────────────────────────────────
def test_connect_providers_connects_each_resolved_provider():
    receipts = connect_providers(_plan(), _registry())
    assert {r.provider for r in receipts} == {"whatsapp_business", "hubspot", "stripe", "slack"}
    assert all(r.connected for r in receipts)


def test_already_connected_is_a_noop_and_missing_adapter_is_reported():
    plan = _plan()
    reg = AdapterRegistry().register(PaperAdapter("hubspot", (_Cap("crm.contact.upsert", 2, True),)))
    receipts = {r.provider: r for r in connect_providers(plan, reg)}
    assert receipts["hubspot"].connected  # registered
    assert not receipts["stripe"].connected and "no adapter" in receipts["stripe"].detail


# ── run the governed test mission ────────────────────────────────────────────────
def test_run_test_mission_executes_and_reconciles_under_envelopes():
    plan = _plan()
    run = run_test_mission(plan, _registry(), requests={
        "chat.message.send": {"channel": "C1", "text": "hi"},
        "crm.contact.upsert": {"email": "a@b.com"},
        "billing.refund.execute": {"charge": "ch_1"},
        "approval.request": {"channel": "#ops"},
    })
    assert run.ok
    assert run.reconciled  # every step's created object re-observed
    # every write step actually ran under an envelope (else the paper adapter would refuse)
    assert all(s.provider_object_id for s in run.steps)


def test_writes_refuse_without_the_runner_supplying_an_envelope():
    # a paper adapter that is NOT marked write would get envelope=None; prove the runner
    # supplies an envelope for a write capability so it proceeds.
    plan = _plan()
    run = run_test_mission(plan, _registry())
    refund = next(s for s in run.steps if s.capability == "billing.refund.execute")
    assert refund.ok and refund.tier == 4  # tier-4 write succeeded under a supplied envelope


def test_unobservable_step_leaves_reconciliation_unknown():
    reg = AdapterRegistry().register(
        PaperAdapter("hubspot", (_Cap("crm.contact.upsert", 2, True),), observable=False))
    plan = plan_from_request("update the crm", reader=KeywordReader.default(), manifest=_manifest(),
                             confirmed_by="a", confirmed_at="t")
    run = run_test_mission(plan, reg, requests={"crm.contact.upsert": {}})
    step = run.steps[0]
    assert step.ok and step.reconciled is None  # UNKNOWN, never fabricated as reconciled


def _confirmed_requiring(capability, provider):
    # Build a confirmed intent DIRECTLY (as a pack pick would), bypassing the reader's
    # grounding — so the compile-time refusal safety net can be exercised.
    graph = CapabilityRequirementGraph(requirements=(CapabilityRequirement(capability, provider),))
    return IntegrationProposal(
        interpreted_outcome="x", requirements=graph,
        provider_preferences={capability: provider},
    ).confirm(confirmed_by="a", confirmed_at="t")


def test_non_connectable_plan_refuses_to_run():
    # a capability nothing can build -> compile refusal -> not connectable -> run refuses
    m = IntegrationManifest(dimensions=(
        CapabilityDimension("chat.message.send", "whatsapp_business", Support.EXECUTED, tier=3),
    ))
    plan = compile_integration_intent(
        _confirmed_requiring("billing.refund.execute", "stripe"), manifest=m)
    assert not plan.connectable and plan.refusals
    with pytest.raises(PlanNotConnectable):
        run_test_mission(plan, _registry())


# ── EXPLAIN ───────────────────────────────────────────────────────────────────────
def test_explain_surfaces_provenance_status_and_reconciliation():
    plan = _plan()
    run = run_test_mission(plan, _registry(), requests={
        "chat.message.send": {"channel": "C1", "text": "hi"},
        "crm.contact.upsert": {}, "billing.refund.execute": {}, "approval.request": {},
    })
    ex = explain(plan, run)
    assert ex.connectable and len(ex.steps) == 4
    refund = next(s for s in ex.steps if s["capability"] == "billing.refund.execute")
    assert refund["tier"] == 4 and refund["status"] == "ok" and refund["reconciled"] is True
    assert refund["provenance"] in {"named", "connected", "preferred", "default"}
    text = ex.to_text()
    assert "billing.refund.execute" in text and "reconciled" in text


def test_explain_lists_refusals_for_a_non_connectable_plan():
    m = IntegrationManifest(dimensions=(
        CapabilityDimension("crm.contact.upsert", "hubspot", Support.EXECUTED, tier=2),
    ))
    plan = compile_integration_intent(
        _confirmed_requiring("billing.refund.execute", "stripe"), manifest=m)
    ex = explain(plan)
    assert not ex.connectable
    assert any(r["capability"] == "billing.refund.execute" for r in ex.refusals)
