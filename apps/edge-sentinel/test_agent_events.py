"""Agent-runtime events become Edge Sentinel cases (the consuming end of the sentinel feed): evidence-backed, idempotent, a signal and never
an action or a closure."""
from __future__ import annotations

import importlib

evidence = importlib.import_module("edge-sentinel.evidence")
case_store = importlib.import_module("edge-sentinel.case_store")
agent_events = importlib.import_module("edge-sentinel.agent_events")

CaseStore = case_store.CaseStore
CaseStatus, CaseType, Severity, FindingStatus = evidence.CaseStatus, evidence.CaseType, evidence.Severity, evidence.FindingStatus

EVENT = {"kind": "LOOP_ESCALATED", "severity": "high", "tenant": "acme", "subject": "loop_1", "source": "goal_loop",
         "detail": "a goal loop could not find a way forward and needs a person", "evidence": {"kind": "repeating_failure"}, "ts": 1_700_000_000.0,
         "key": "k1"}


def test_an_event_becomes_a_case_with_immutable_evidence_an_observation_and_a_finding():
    st = CaseStore()
    case = agent_events.ingest_agent_event(st, EVENT)
    assert case.case_type is CaseType.AI_SECURITY and case.severity is Severity.HIGH and case.status is CaseStatus.INVESTIGATING
    assert "LOOP_ESCALATED" in case.title and "loop_1" in case.title
    (eid,) = case.evidence_refs
    assert st.evidence[eid].kind == "agent_event" and st.get_raw(st.evidence[eid]) == evidence.canonical_json(EVENT)      # the raw event, verbatim
    obs = st.observations[case.observation_refs[0]]
    assert obs.source_type == "runtime" and obs.normalized_fields["subject"] == "loop_1" and obs.identity_refs == ("subject:loop_1",)
    f = st.findings[case.finding_refs[0]]
    assert f.status is FindingStatus.SUPPORTED and f.evidence_refs == (eid,) and f.validate()


def test_an_event_is_a_signal_not_an_action_or_a_closure():
    st = CaseStore()
    case = agent_events.ingest_agent_event(st, EVENT)
    assert case.action_refs == () and st.actions == {} and st.decisions == {} and st.receipts == {}
    assert st.findings[case.finding_refs[0]].recommended_actions == () and case.closure == ""
    assert case.status is not CaseStatus.CLOSED


def test_ingesting_the_same_event_again_does_not_duplicate_anything():
    st = CaseStore()
    a = agent_events.ingest_agent_event(st, EVENT)
    b = agent_events.ingest_agent_event(st, dict(EVENT))
    assert a.case_id == b.case_id and len(st.cases) == len(st.evidence) == len(st.observations) == len(st.findings) == 1


def test_a_different_event_is_a_different_case():
    st = CaseStore()
    a = agent_events.ingest_agent_event(st, EVENT)
    b = agent_events.ingest_agent_event(st, {**EVENT, "subject": "loop_2", "key": "k2"})
    assert a.case_id != b.case_id and len(st.cases) == 2


def test_events_below_the_floor_open_no_case():
    st = CaseStore()
    low = {**EVENT, "kind": "CHILD_SPAWNED", "severity": "info"}
    assert agent_events.ingest_agent_event(st, low) is None and st.cases == {} and st.evidence == {}
    assert agent_events.ingest_agent_event(st, low, min_severity="info") is not None
    assert agent_events.ingest_agent_event(CaseStore(), {**EVENT, "severity": "medium"}, min_severity="high") is None


def test_critical_events_keep_their_severity():
    st = CaseStore()
    c = agent_events.ingest_agent_event(st, {**EVENT, "kind": "SCOPE_TAMPERED", "severity": "critical", "subject": "m1", "source": "context"})
    assert c.severity is Severity.CRITICAL


def test_pull_cases_opens_cases_from_a_feed_and_resumes_from_its_cursor():
    from agentic_os.mission.sentinel import SentinelFeed
    from agentic_os.mission.store import EventStore
    feed = SentinelFeed(EventStore(), clock=lambda: 100.0)
    st = CaseStore()
    feed.loop_sink({"type": "LOOP_ESCALATED", "loop_id": "l1", "kind": "no_progress"})
    feed.loop_sink({"type": "LOOP_COMPLETED", "loop_id": "l1"})                          # info: no case
    feed.child_sink({"type": "CHILD_REVOKED", "child_id": "c1", "principal": "agent:x"})
    cases, cur = agent_events.pull_cases(st, feed, 0)
    assert len(cases) == 2 and {c.severity for c in cases} == {Severity.HIGH}
    again, cur2 = agent_events.pull_cases(st, feed, cur)
    assert again == [] and cur2 == cur
    feed.egress({"ts": 3.0, "client": "c9", "host": "evil.example", "port": 443, "allow": False, "rule": "default-deny"})
    new, cur3 = agent_events.pull_cases(st, feed, cur2)
    assert len(new) == 1 and "EGRESS_DENIED" in new[0].title and len(st.cases) == 3
    replay, _ = agent_events.pull_cases(CaseStore(), feed, 0)                              # a fresh store can be rebuilt from the feed alone
    assert len(replay) == 3
