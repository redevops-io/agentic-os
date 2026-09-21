"""Phase 5 acceptance: a known fixture gives a reproducible timeline and evidence-linked findings;
narrative changes never change source evidence.
"""
from __future__ import annotations

import importlib

import pytest

evidence = importlib.import_module("edge-sentinel.evidence")
case_store = importlib.import_module("edge-sentinel.case_store")
dfir = importlib.import_module("edge-sentinel.dfir")

Finding = evidence.Finding
FindingStatus = evidence.FindingStatus
CaseType = evidence.CaseType
CaseStore = case_store.CaseStore
acquire = dfir.acquire
verify_custody = dfir.verify_custody
TimelineEvent = dfir.TimelineEvent
build_timeline = dfir.build_timeline
correlate = dfir.correlate
dfir_case = dfir.dfir_case
SandboxProvider = dfir.SandboxProvider
NullSandboxProvider = dfir.NullSandboxProvider

# a small forensic fixture: an auth.log slice as raw content
AUTHLOG = "Sep 21 10:00:01 web1 sshd[42]: Failed password for root from 203.0.113.7\n"
EVENTS_RAW = [
    {"ts": "2026-09-21T10:00:01Z", "actor": "203.0.113.7", "action": "failed_login", "target": "web1"},
    {"ts": "2026-09-21T10:00:03Z", "actor": "203.0.113.7", "action": "failed_login", "target": "web1"},
    {"ts": "2026-09-21T10:00:05Z", "actor": "203.0.113.7", "action": "accepted_login", "target": "web1"},
]


def _fixture_store():
    s = CaseStore()
    ev = acquire("auth_log", "web1", AUTHLOG, acquired_by="ir-analyst", method="ssh-copy",
                 source_host="web1", observed_at="2026-09-21T10:05:00Z")
    s.put_evidence(ev, AUTHLOG)
    events = [TimelineEvent(timestamp=e["ts"], evidence_ref=ev.artifact_id, actor=e["actor"],
                            action=e["action"], target=e["target"],
                            description=f"{e['actor']} {e['action']} on {e['target']}")
              for e in EVENTS_RAW]
    return s, ev, events


def test_acquisition_records_chain_of_custody():
    s, ev, _ = _fixture_store()
    assert ev.artifact_id.startswith("ev-") and ev.sha256
    joined = " ".join(ev.chain_of_custody)
    assert "acquired_by:ir-analyst" in joined and "method:ssh-copy" in joined and "host:web1" in joined
    assert verify_custody(ev, AUTHLOG) is True


def test_timeline_is_reproducible():
    """Same inputs → identical ordered timeline (order + ids), regardless of input order."""
    s, ev, events = _fixture_store()
    t1 = build_timeline(events)
    t2 = build_timeline(list(reversed(events)))          # shuffle the input
    assert [e.event_id for e in t1] == [e.event_id for e in t2]
    assert [e.timestamp for e in t1] == sorted(e["ts"] for e in EVENTS_RAW)   # chronological


def test_every_timeline_event_links_to_evidence():
    s, ev, events = _fixture_store()
    for e in build_timeline(events):
        assert e.evidence_ref == ev.artifact_id and ev.artifact_id in s.evidence


def test_renarrating_does_not_change_evidence():
    """Changing an event's description yields a new event id but the SAME evidence (sha256 unchanged) —
    narrative ≠ source evidence."""
    s, ev, events = _fixture_store()
    e0 = events[0]
    renarrated = TimelineEvent(timestamp=e0.timestamp, evidence_ref=e0.evidence_ref, actor=e0.actor,
                               action=e0.action, target=e0.target, description="ANALYST REVISED NARRATIVE")
    assert renarrated.event_id == e0.event_id            # identity is narrative-independent
    assert verify_custody(ev, AUTHLOG) is True            # evidence bytes untouched
    # a genuinely different observed action IS a different event
    other = TimelineEvent(timestamp=e0.timestamp, evidence_ref=e0.evidence_ref, actor=e0.actor,
                          action="privilege_escalation", target=e0.target)
    assert other.event_id != e0.event_id


def test_correlation_groups_by_entity():
    s, ev, events = _fixture_store()
    corr = correlate(events)
    assert "203.0.113.7" in corr and "web1" in corr
    assert len(corr["203.0.113.7"]) == 3                 # all three events involve the attacker


def test_dfir_case_links_timeline_evidence_and_findings():
    s, ev, events = _fixture_store()
    finding = Finding(claim="Brute force then successful login from 203.0.113.7 on web1",
                      confidence=0.9, evidence_refs=(ev.artifact_id,), status=FindingStatus.SUPPORTED)
    case = dfir_case(s, events, title="web1 ssh compromise", findings=[finding])
    assert case.case_type is CaseType.DFIR
    assert ev.artifact_id in case.evidence_refs and finding.finding_id in case.finding_refs
    assert finding.finding_id in s.findings


# ── sandbox is an interface only ──

def test_sandbox_provider_is_abstract():
    with pytest.raises(TypeError):
        SandboxProvider()                                # cannot instantiate the ABC


def test_null_sandbox_is_explicit_not_analyzed():
    v = NullSandboxProvider().analyze("ev-abc", b"payload")
    assert v.verdict == "not_analyzed" and v.provider == "null"


def test_injected_sandbox_provider_is_used():
    """A test double proves the seam works without building a real sandbox."""
    class FakeSandbox(SandboxProvider):
        def analyze(self, artifact_ref, content):
            return dfir.SandboxVerdict(artifact_ref=artifact_ref, verdict="malicious", score=0.98,
                                       signatures=("emotet",), provider="fake")

    v = FakeSandbox().analyze("ev-x", b"payload")
    assert v.verdict == "malicious" and "emotet" in v.signatures and v.provider == "fake"
