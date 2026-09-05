"""AGPL evidence producers — project durable runtime streams into ControlEvidence (stream-keyed).

Uses the real append-only EventStore and a tiny fake registry. The Enterprise edition maps these
stream-keyed rows onto framework controls; here we only assert the base emits honest evidence.
"""
from __future__ import annotations

import pytest

pytest.importorskip("runtime_contracts")
from runtime_contracts import ControlEvidence, ControlStatus, RuntimeStream

from agentic_os.compliance import RuntimeEvidenceProducer, mission_evidence, system_evidence
from agentic_os.mission.store import EventStore


class _Spec:
    def __init__(self, name, operator="op", data_classifications=()):
        self.name = name
        self.operator = operator
        self.data_classifications = tuple(data_classifications)


class _Registry:
    def __init__(self, specs):
        self._specs = list(specs)

    def all(self):
        return list(self._specs)


def _store_with_mission(mid="m1"):
    s = EventStore()
    s.append("MissionCreated", mid, {"brief": "x"})
    s.append("NodeCompleted", mid, {"node_id": "n1"})
    return s


def test_durable_event_ledger_is_enforced_when_events_exist():
    s = _store_with_mission()
    p = RuntimeEvidenceProducer(events=s)
    ev = p.durable_event_ledger("m1")
    assert len(ev) == 1
    assert ev[0].control_id == RuntimeStream.DURABLE_EVENT_LEDGER.value
    assert ev[0].status is ControlStatus.ENFORCED
    assert ev[0].subject == "m1"
    assert ev[0].event_ids  # cites the ledger rows
    assert ev[0].evidence_id.startswith("rcv1:")


def test_no_events_produces_nothing_not_a_false_pass():
    p = RuntimeEvidenceProducer(events=EventStore())
    assert p.durable_event_ledger("ghost") == []
    assert p.for_mission("ghost") == []


def test_hitl_approval_evidenced_only_when_a_human_acted():
    s = _store_with_mission()
    assert RuntimeEvidenceProducer(events=s).hitl_approval("m1") == []  # no approvals yet
    s.append("ApprovalGranted", "m1", {"node_id": "n1", "edit": None, "capability": "publish"})
    ev = RuntimeEvidenceProducer(events=s).hitl_approval("m1")
    assert len(ev) == 1
    assert ev[0].control_id == RuntimeStream.HITL_APPROVAL.value
    assert ev[0].status is ControlStatus.EVIDENCED
    assert ev[0].event_ids == ("ApprovalGranted#3",)


def test_authority_grant_from_governance_events():
    s = _store_with_mission()
    s.append("PolicyChanged", "m1", {"actor": "gov", "reason": "tighten"})
    ev = RuntimeEvidenceProducer(events=s).authority_grant("m1")
    assert ev and ev[0].control_id == RuntimeStream.AUTHORITY_GRANT.value
    assert ev[0].status is ControlStatus.EVIDENCED


def test_evidence_lineage_and_verification():
    s = _store_with_mission()
    s.append("ClaimRaised", "m1", {"statement": "s"})
    s.append("EvidenceRecorded", "m1", {"claim_id": "c1"})
    s.append("VerificationRecorded", "m1", {"decision": "ACCEPT"})
    p = RuntimeEvidenceProducer(events=s)
    lin = p.evidence_lineage("m1")
    ver = p.verification_receipt("m1")
    assert lin[0].control_id == RuntimeStream.EVIDENCE_LINEAGE.value
    assert len(lin[0].event_ids) == 2
    assert ver[0].control_id == RuntimeStream.VERIFICATION_RECEIPT.value
    assert ver[0].status is ControlStatus.EVIDENCED


def test_capability_manifest_enforced_when_registered():
    reg = _Registry([_Spec("render"), _Spec("publish")])
    ev = system_evidence(reg)
    manifest = [e for e in ev if e.control_id == RuntimeStream.CAPABILITY_MANIFEST.value]
    assert manifest and manifest[0].status is ControlStatus.ENFORCED
    assert manifest[0].subject == "runtime"
    assert set(manifest[0].artifact_refs) == {"capability:render", "capability:publish"}


def test_data_flow_classification_only_for_declaring_capabilities():
    reg = _Registry([_Spec("a"), _Spec("b", data_classifications=("pii",))])
    ev = system_evidence(reg)
    dfc = [e for e in ev if e.control_id == RuntimeStream.DATA_FLOW_CLASSIFICATION.value]
    assert len(dfc) == 1
    assert dfc[0].status is ControlStatus.EVIDENCED
    assert dfc[0].artifact_refs == ("capability:b",)


def test_data_flow_classification_absent_when_none_declared():
    reg = _Registry([_Spec("a"), _Spec("b")])
    dfc = [e for e in system_evidence(reg)
           if e.control_id == RuntimeStream.DATA_FLOW_CLASSIFICATION.value]
    assert dfc == []


def test_all_covers_every_mission_plus_system():
    s = _store_with_mission("m1")
    s.append("ApprovalGranted", "m1", {"node_id": "n1"})
    s.append("MissionCreated", "m2", {"brief": "y"})
    reg = _Registry([_Spec("x", data_classifications=("internal",))])
    ev = RuntimeEvidenceProducer(events=s, registry=reg).all()
    streams = {e.control_id for e in ev}
    assert RuntimeStream.DURABLE_EVENT_LEDGER.value in streams   # m1 and m2
    assert RuntimeStream.HITL_APPROVAL.value in streams          # m1 only
    assert RuntimeStream.CAPABILITY_MANIFEST.value in streams    # system
    assert RuntimeStream.DATA_FLOW_CLASSIFICATION.value in streams
    # exactly one ledger row per mission that has events
    ledgers = [e for e in ev if e.control_id == RuntimeStream.DURABLE_EVENT_LEDGER.value]
    assert {e.subject for e in ledgers} == {"m1", "m2"}


def test_mission_evidence_convenience_matches_producer():
    s = _store_with_mission()
    a = mission_evidence(s, "m1")
    b = RuntimeEvidenceProducer(events=s).for_mission("m1")
    assert [e.evidence_id for e in a] == [e.evidence_id for e in b]


def test_evidence_is_content_addressed_and_deterministic():
    s = _store_with_mission()
    e1 = RuntimeEvidenceProducer(events=s).durable_event_ledger("m1")[0]
    e2 = RuntimeEvidenceProducer(events=s).durable_event_ledger("m1")[0]
    assert e1.evidence_id == e2.evidence_id
    assert isinstance(e1, ControlEvidence)
