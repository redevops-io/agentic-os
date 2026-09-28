"""Content Search-Intelligence signals — the §3 categories, evidence-gated (§9)."""
from __future__ import annotations

from agentic_os.content.search_signals import (
    SearchObservation, SignalType, cannibalization, ctr_opportunity, emergent_intent, missing_page, near_win)


def _obs(**kw) -> SearchObservation:
    base = dict(query="agent memory", page_url="https://x.io/agent-memory/", impressions=200, clicks=4,
                ctr=0.02, position=8.0, relevance=0.85, days_observed=20)
    base.update(kw)
    return SearchObservation(**base)


def test_near_win_needs_position_impressions_and_relevance() -> None:
    s = near_win(_obs(position=8.0))
    assert s and s.signal_type is SignalType.NEAR_WIN and s.status == "PROPOSE"
    assert near_win(_obs(position=3.0)) is None            # already ranking well
    assert near_win(_obs(impressions=5)) is None           # sample too small
    assert near_win(_obs(relevance=0.2)) is None           # position alone must never trigger (§9)


def test_near_win_watch_before_observation_window() -> None:
    assert near_win(_obs(days_observed=3)).status == "WATCH"


def test_emergent_intent_is_watch_only() -> None:
    s = emergent_intent(_obs(impressions=15, relevance=0.95), baseline_impressions=0)
    assert s and s.signal_type is SignalType.EMERGENT_INTENT and s.status == "WATCH"
    assert emergent_intent(_obs(impressions=15, relevance=0.7), baseline_impressions=0) is None   # fit too low
    assert emergent_intent(_obs(impressions=15, relevance=0.95), baseline_impressions=500) is None  # not new


def test_missing_page_on_noncanonical_url() -> None:
    s = missing_page(_obs(page_url="https://x.io/blog/your-ai-does-not-have-memory/"))
    assert s and s.signal_type is SignalType.MISSING_PAGE and "CREATE_CANONICAL_PAGE" in s.proposed_action
    # a canonical page already ranking → no signal
    assert missing_page(_obs(page_url="https://x.io/agent-memory/")) is None


def test_ctr_opportunity_when_ctr_under_baseline() -> None:
    s = ctr_opportunity(_obs(position=3.0, impressions=500, ctr=0.02))   # ~0.10 expected at pos 3
    assert s and s.signal_type is SignalType.CTR_OPPORTUNITY
    assert ctr_opportunity(_obs(position=3.0, impressions=500, ctr=0.09)) is None   # near baseline
    assert ctr_opportunity(_obs(position=25.0, impressions=500, ctr=0.001)) is None  # ranking not reasonable


def test_cannibalization_two_pages_same_intent() -> None:
    obs = [_obs(page_url="https://x.io/a/"), _obs(page_url="https://x.io/b/"), _obs(query="other", page_url="https://x.io/c/")]
    sigs = cannibalization(obs)
    assert len(sigs) == 1 and sigs[0].signal_type is SignalType.CANNIBALIZATION
    assert set(sigs[0].affected_pages) == {"https://x.io/a/", "https://x.io/b/"}
