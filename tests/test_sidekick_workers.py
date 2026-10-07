"""Multi-Sidekick worker results + deterministic merge (plan §24D–§24N).

Proves: agreeing claims deduplicate into one; disagreeing claims are PRESERVED as conflicts (never averaged or
voted away); evidence/artifacts union; candidate actions rank deterministically; stale worker results are excluded
by context_version; the merge is deterministic for the same inputs; and the receipt explains the merge.
"""
from __future__ import annotations

from agentic_os.sidekick import (
    ArtifactLink, Claim, ClaimConflict, MergePolicy, MergedSidekickResult, SidekickWorkerResult,
    WorkerResultType, merge_worker_results,
)
from agentic_os.mission.merge import Severity


def _wr(worker, claims=(), **kw):
    return SidekickWorkerResult(worker_id=worker, capability_id=kw.pop("cap", "sales.deal_close"),
                                claims=tuple(claims), **kw)


def test_agreeing_claims_dedup_into_one_with_unioned_evidence():
    a = Claim("acme", "stage", "negotiation", evidence_refs=("e1",), confidence=0.6, worker_id="w1")
    b = Claim("acme", "stage", "negotiation", evidence_refs=("e2",), confidence=0.8, worker_id="w2")
    m = merge_worker_results([_wr("w1", [a]), _wr("w2", [b])])
    assert len(m.agreed_claims) == 1
    c = m.agreed_claims[0]
    assert c.value == "negotiation" and c.evidence_refs == ("e1", "e2") and c.confidence == 0.8
    assert m.conflicting_claims == ()
    assert m.receipt.deduplicated_claims == 1


def test_disagreeing_claims_are_preserved_not_resolved():
    a = Claim("acme", "close_month", "this_month", confidence=0.8, worker_id="crm")
    b = Claim("acme", "close_month", "next_month", confidence=0.85, worker_id="quote")
    m = merge_worker_results([_wr("crm", [a]), _wr("quote", [b])])
    assert m.agreed_claims == ()
    assert len(m.conflicting_claims) == 1
    conflict = m.conflicting_claims[0]
    assert isinstance(conflict, ClaimConflict)
    assert {c.value for c in conflict.claims} == {"this_month", "next_month"}
    assert conflict.severity in (Severity.HIGH, Severity.CRITICAL)   # high-confidence disagreement
    assert conflict.resolution_required is True
    assert m.receipt.conflicts_detected == 1 and m.receipt.conflicts_remaining == 1
    assert m.confidence == 0.0   # 0 agreed / 1 conflict


def test_highest_evidence_policy_can_resolve_when_evidence_decisive():
    a = Claim("acme", "owner", "sarah", evidence_refs=("e1", "e2", "e3"), worker_id="w1")
    b = Claim("acme", "owner", "john", evidence_refs=("e9",), worker_id="w2")
    m = merge_worker_results([_wr("w1", [a]), _wr("w2", [b])],
                             policies={"claims": MergePolicy.HIGHEST_EVIDENCE})
    assert [c.value for c in m.agreed_claims] == ["sarah"]
    assert m.conflicting_claims == ()


def test_evidence_and_artifacts_union_dedup():
    art = ArtifactLink(provider="metabase", resource_type="dashboard", resource_id="42")
    r1 = _wr("w1", evidence_refs=("e1", "e2"), artifacts=(art,))
    r2 = _wr("w2", evidence_refs=("e2", "e3"), artifacts=(art,))   # same artifact
    m = merge_worker_results([r1, r2])
    assert m.evidence == ("e1", "e2", "e3")
    assert len(m.artifacts) == 1


def test_candidate_actions_rank_by_score_and_dedup():
    r1 = _wr("w1", candidate_actions=({"action_type": "send_quote", "score": 0.4},
                                      {"action_type": "wait", "score": 0.9}))
    r2 = _wr("w2", candidate_actions=({"action_type": "wait", "score": 0.9},   # dup key
                                      {"action_type": "escalate", "score": 0.7}))
    m = merge_worker_results([r1, r2])
    order = [a["action_type"] for a in m.candidate_actions]
    assert order == ["wait", "escalate", "send_quote"]   # by score desc, deduped


def test_stale_results_excluded_by_context_version():
    fresh = _wr("w_fresh", [Claim("acme", "stage", "won", worker_id="w_fresh")], context_version=5)
    stale = _wr("w_stale", [Claim("acme", "stage", "lost", worker_id="w_stale")], context_version=3)
    m = merge_worker_results([fresh, stale], current_context_version=5)
    assert m.contributing_workers == ("w_fresh",)
    assert m.receipt.excluded_workers == ("w_stale",)
    assert "stale" in m.receipt.exclusion_reasons["w_stale"]
    # the stale 'lost' claim never entered the merge → no conflict, agreed 'won'
    assert [c.value for c in m.agreed_claims] == ["won"]


def test_merge_is_deterministic():
    rs = [_wr("w2", [Claim("x", "p", "b", worker_id="w2")]),
          _wr("w1", [Claim("x", "p", "a", worker_id="w1")])]
    m1 = merge_worker_results(rs, session_id="s", mission_id="m")
    m2 = merge_worker_results(list(reversed(rs)), session_id="s", mission_id="m")
    assert m1.contributing_workers == m2.contributing_workers == ("w1", "w2")
    assert m1.conflicting_claims[0].claims == m2.conflicting_claims[0].claims  # stable claim order


def test_state_changes_keyed_merge_flags_conflict():
    r1 = _wr("w1", proposed_state_changes=({"target": "opp:acme", "field": "stage", "value": "won"},))
    r2 = _wr("w2", proposed_state_changes=({"target": "opp:acme", "field": "stage", "value": "lost"},))
    m = merge_worker_results([r1, r2])
    assert any("state conflict on opp:acme.stage" == q for q in m.unresolved_questions)


def test_receipt_records_policies_and_is_present():
    m = merge_worker_results([_wr("w1", [Claim("a", "b", "c", worker_id="w1")])])
    assert isinstance(m, MergedSidekickResult) and m.receipt is not None
    assert m.receipt.policies_applied["claims"] == "keep_contradictions"
    assert m.receipt.policies_applied["candidate_actions"] == "rank_candidates"
