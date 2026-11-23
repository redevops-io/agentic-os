"""SR-04 round 5 (DOC-1, V6, L6): documented default-policy abstention, a RelevanceResult never carries a labelled
source with a value, and the attestation MAC binds the source."""
from __future__ import annotations

import pathlib

import pytest

from agentic_os.content.interventions import content_intervention_queue
from agentic_os.content.relevance import RelevanceResult
from agentic_os.content.search_signals import SearchObservation, near_win
from agentic_os.priority_engine import PriorityPolicy

DOC = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "SEARCH_RELEVANCE.md").read_text()


def hooked(v=0.95):
    return SearchObservation(query="agent memory", page_url="https://x.io/blog/p", impressions=500, ctr=0.04, position=8.0,
                             relevance=v, days_observed=28, site_id="s", relevance_source="embedding:m@1",
                             relevance_detail="embedder=m")


def test_doc1_default_policy_abstains_with_count_zero_and_permissive_policies_only_request_approval():
    s = near_win(hooked())
    assert s.status == "PROPOSE" and s.confidence == 0.5
    q = content_intervention_queue([s])
    assert q["count"] == 0 and q["auto"] == 0 and q["requires_approval"] == 0         # default: ABSTAIN
    for pol in (PriorityPolicy(min_confidence=0.0), PriorityPolicy(min_confidence=0.0, allow_auto_bounded_writes=True)):
        q = content_intervention_queue([s], policy=pol)
        assert q["count"] == 1 and q["auto"] == 0 and q["requires_approval"] == 1     # REQUEST_APPROVAL only
    low = " ".join(DOC.split())
    assert "ABSTAINS from the action queue" in low and "`count` 0" in low and "REQUEST_APPROVAL" in low


@pytest.mark.parametrize("source", ["supplied:crm", "supplied:embedding-hook", "measured", "manual", "unset", "",
                                    "human:rated", "buyer:panel", "embedding", "unavailable"])
def test_v6_a_relevance_result_never_accepts_a_labelled_or_unset_source_with_a_value(source):
    for value in (0.0, 0.5, 1.0):
        with pytest.raises(ValueError):
            RelevanceResult(value, source, "m", "r")
    with pytest.raises(ValueError):
        RelevanceResult(None, source, "m", "r")


def test_v6_only_embedding_with_a_value_and_unavailable_without_one_are_accepted():
    assert RelevanceResult(0.0, "embedding:m@1", "m", "1").value == 0.0
    assert RelevanceResult(None, "unavailable:no_page_text").value is None
    with pytest.raises(ValueError):
        RelevanceResult(0.5, "unavailable:x", "m", "1")
    with pytest.raises(ValueError):
        RelevanceResult(None, "embedding:m@1", "m", "1")
