"""Phase 3 acceptance: labelled fixtures require multi-source evidence; unsupported conclusions abstain;
queries validate before execution.
"""
from __future__ import annotations

import importlib

import pytest

evidence = importlib.import_module("edge-sentinel.evidence")
case_store = importlib.import_module("edge-sentinel.case_store")
hunt = importlib.import_module("edge-sentinel.hunt")

EvidenceArtifact = evidence.EvidenceArtifact
SecurityObservation = evidence.SecurityObservation
canonical_json = evidence.canonical_json
CaseStore = case_store.CaseStore
Hypothesis = hunt.Hypothesis
HypothesisStatus = hunt.HypothesisStatus
Query = hunt.Query
validate_query = hunt.validate_query
assess_hypothesis = hunt.assess_hypothesis
run_hunt = hunt.run_hunt


def _obs(store, source_type, fields):
    """Store an observation from a given source type (behind immutable evidence)."""
    raw = canonical_json({"source_type": source_type, **fields})
    ev = store.put_evidence(EvidenceArtifact.of_raw("event", source_type, raw), raw)
    o = SecurityObservation(source=source_type, source_type=source_type, observed_at="t", known_at="t",
                            raw_evidence_ref=ev.artifact_id, normalized_fields=fields)
    return store.put_observation(o)


# ── evidence sufficiency: multi-source or abstain ──

def test_single_source_hypothesis_abstains():
    """One source is not enough — the conclusion abstains (INSUFFICIENT), never CONFIRMED."""
    s = CaseStore()
    o1 = _obs(s, "crowdsec", {"ip": "203.0.113.7"})
    o2 = _obs(s, "crowdsec", {"ip": "203.0.113.7"})   # same source type
    hyp = Hypothesis(statement="203.0.113.7 is actively attacking us",
                     supporting_obs=(o1.id, o2.id))
    assert assess_hypothesis(hyp, s).status is HypothesisStatus.INSUFFICIENT


def test_multi_source_hypothesis_is_supported():
    """Corroboration across DISTINCT source types clears the bar."""
    s = CaseStore()
    o1 = _obs(s, "crowdsec", {"ip": "203.0.113.7"})
    o2 = _obs(s, "wazuh", {"ip": "203.0.113.7", "failed_logins": 40})
    hyp = Hypothesis(statement="203.0.113.7 is actively attacking us",
                     supporting_obs=(o1.id, o2.id))
    assert assess_hypothesis(hyp, s).status is HypothesisStatus.CONFIRMED


def test_contradiction_outweighs_support():
    s = CaseStore()
    o1 = _obs(s, "crowdsec", {"ip": "x"})
    c1 = _obs(s, "wazuh", {"ip": "x", "benign": True})
    c2 = _obs(s, "inspector", {"ip": "x", "benign": True})
    hyp = Hypothesis(statement="x is malicious", supporting_obs=(o1.id,),
                     contradicting_obs=(c1.id, c2.id))
    assert assess_hypothesis(hyp, s).status is HypothesisStatus.CONTRADICTED


def test_no_evidence_abstains():
    s = CaseStore()
    assert assess_hypothesis(Hypothesis(statement="unfounded claim"), s).status is HypothesisStatus.INSUFFICIENT


# ── query validation before execution ──

def test_read_only_query_validates():
    ok, reason = validate_query(Query(backend="wazuh", text="search failed_logins by source_ip"))
    assert ok and reason == "ok"


def test_write_query_is_rejected():
    for bad in ("DELETE from decisions", "drop table alerts", "block ip 1.2.3.4",
                "update users set admin=1", "exec sp_who"):
        ok, reason = validate_query(Query(backend="sql", text=bad))
        assert not ok


def test_query_declared_not_read_only_is_rejected():
    ok, reason = validate_query(Query(backend="sql", text="select 1", read_only=False))
    assert not ok and "read-only" in reason


def test_empty_query_is_rejected():
    assert validate_query(Query(backend="sql", text="  "))[0] is False


# ── the hunt runner only executes validated read-only queries ──

def test_hunt_runs_only_validated_queries_and_flags_negative_result():
    s = CaseStore()
    o1 = _obs(s, "crowdsec", {"ip": "203.0.113.7"})     # single source → will abstain
    hyp = Hypothesis(statement="lateral movement from 203.0.113.7", supporting_obs=(o1.id,))
    ran = []
    queries = [
        Query(backend="wazuh", text="search process_create by host"),     # ok
        Query(backend="sql", text="DELETE from audit"),                    # rejected, must NOT run
    ]
    res = run_hunt(hyp, queries, s, executor=lambda q: ran.append(q.query_id) or "rows:0")
    assert len(res.validated_queries) == 1 and len(res.rejected_queries) == 1
    assert len(ran) == 1                                    # the write query never executed
    assert res.negative_result is True                      # single-source hypothesis abstained
    assert res.hypothesis.status is HypothesisStatus.INSUFFICIENT


def test_hunt_dry_run_executes_nothing():
    s = CaseStore()
    hyp = Hypothesis(statement="h")
    res = run_hunt(hyp, [Query(backend="wazuh", text="search x")], s)   # executor=None
    assert res.validated_queries and res.ran == []
