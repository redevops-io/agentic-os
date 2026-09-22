"""Phase B — a governed Integration-Plane run projects canonical evidence + receipts.

Proves the seam end-to-end: a plan runs through the real runner (governed envelopes + reconciliation),
and ``govern_run`` turns the outcome into typed business objects + canonical ActionReceipts with
independent verification — no provider-specific logic outside the connector payloads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

from agentic_os.integrations import (
    AdapterRegistry, CapabilityDimension, IntegrationManifest, KeywordReader, Support,
    plan_from_request, run_test_mission)
from agentic_os.integrations.execution import MissionRun, StepRun
from agentic_os.integrations.business import (
    Contact, Message, Refund, VerificationState, canonical_evidence, govern_run, receipts_for_run)


# ── a paper adapter that returns realistic provider payloads (so normalization has data) ──
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
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Obs:
    found: bool


# per-(provider,capability) realistic payloads returned by execute()
_PAYLOADS = {
    ("hubspot", "crm.contact.upsert"): {"id": "701", "properties": {"email": "a@b.com", "firstname": "Jane", "lastname": "Doe"}},
    ("stripe", "billing.refund.execute"): {"id": "re_1", "object": "refund", "charge": "ch_1", "amount": 1099, "currency": "usd", "status": "succeeded"},
    ("slack", "approval.request"): {"ts": "1699999999.001", "channel": "C1", "text": "approve refund?"},
    ("whatsapp_business", "chat.message.send"): {"message_id": "wamid.X", "status": "sent"},   # no normalizer → skipped
}


@dataclass
class PaperAdapter:
    provider: str
    caps: Tuple[_Cap, ...]
    _created: set = field(default_factory=set)

    def capabilities(self): return self.caps
    def connect(self, config, credential_ref): return _Obs(found=True)
    def health(self): return _Obs(found=True)

    def execute(self, capability, request, envelope) -> _Result:
        cap = next((c for c in self.caps if c.name == capability), None)
        if cap is None:
            return _Result(ok=False, error="unsupported")
        if cap.write and envelope is None:
            return _Result(ok=False, error="envelope required")
        oid = f"{self.provider}:{capability}:1"
        self._created.add(oid)
        return _Result(ok=True, provider_object_id=oid, data=_PAYLOADS.get((self.provider, capability), {}))

    def observe(self, resource_ref) -> _Obs:
        return _Obs(found=resource_ref in self._created)


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


def _run() -> MissionRun:
    plan = plan_from_request(
        "answer whatsapp support, update the crm, refund via stripe, approve in slack",
        reader=KeywordReader.default(), manifest=_manifest(),
        confirmed_by="alex", confirmed_at="2026-09-22T00:00:00Z",
        answers={"Who is allowed to approve refunds?": "Finance", "Which account or number should send replies?": "+1"})
    return run_test_mission(plan, _registry(), requests={
        "chat.message.send": {"text": "hi"}, "crm.contact.upsert": {"email": "a@b.com"},
        "billing.refund.execute": {"charge": "ch_1"}, "approval.request": {"channel": "#ops"}})


def test_governed_run_projects_canonical_evidence():
    ev = canonical_evidence(_run())
    kinds = {type(o).__name__ for o in ev}
    assert {"Contact", "Refund", "Message"} <= kinds     # normalized from hubspot/stripe/slack payloads
    contact = next(o for o in ev if isinstance(o, Contact))
    assert contact.email == "a@b.com" and contact.prov.provider == "hubspot"
    refund = next(o for o in ev if isinstance(o, Refund))
    assert refund.charge_ref == "ch_1" and refund.amount_cents == 1099


def test_governed_run_emits_canonical_receipts_with_verification():
    receipts = receipts_for_run(_run(), decision_ids={"billing.refund.execute": "dec-refund-1"})
    by_cap = {s.capability: s for s in receipts}
    # every governed WRITE produced a receipt; reads would not (there are no read steps here)
    assert set(by_cap) == {"chat.message.send", "crm.contact.upsert", "billing.refund.execute", "approval.request"}
    refund = by_cap["billing.refund.execute"]
    assert refund.receipt.status == "SUCCEEDED" and refund.verification is VerificationState.VERIFIED
    assert refund.receipt.decision_id == "dec-refund-1" and refund.receipt.provider == "stripe"


def test_govern_run_bundle():
    result = govern_run(_run())
    assert result.all_writes_verified
    d = result.to_dict()
    assert d["intent_hash"] and len(d["receipts"]) == 4 and len(d["evidence"]) >= 3


# ── unit level: unconfirmed / refuted / unknown map correctly from a synthetic run ──
def test_receipts_reflect_verification_honestly():
    run = MissionRun("ih", (
        StepRun("crm.contact.upsert", "hubspot", 2, ok=True, provider_object_id="c1", reconciled=True,
                data={"id": "c1", "properties": {"email": "x@y.com"}}, write=True),
        StepRun("chat.message.send", "slack", 3, ok=True, provider_object_id="m1", reconciled=False,
                data={"ts": "1", "text": "hi"}, write=True),                       # provider ok, reread refuted
        StepRun("billing.refund.execute", "stripe", 4, ok=False, error="declined", write=True),  # failed
        StepRun("billing.charge.find", "stripe", 1, ok=True, provider_object_id="ch1", reconciled=True,
                data={"object": "charge", "amount": 500, "currency": "usd", "status": "succeeded"}, write=False),
    ))
    receipts = {s.capability: s for s in receipts_for_run(run)}
    assert "billing.charge.find" not in receipts                     # read step → evidence, not a receipt
    assert receipts["crm.contact.upsert"].receipt.status == "SUCCEEDED"
    assert receipts["chat.message.send"].receipt.status == "HELD"    # refuted → not claimed as success
    assert receipts["billing.refund.execute"].receipt.status == "FAILED"
    # the read charge still becomes canonical evidence
    assert any(type(o).__name__ == "Charge" for o in canonical_evidence(run))
