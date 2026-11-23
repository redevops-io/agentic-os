"""SR-04 round 5 (VL-1): with the flag OFF an observation whose relevance_source is INVALID (malformed, over-long,
synthetic-labelled such as `ssr:buyer-score`) is no longer judged on its number: no legacy producer can have set
`relevance_source` (the field is new), so it reads exactly like `unavailable:*` -- the value is ignored (silent flag
off, not assessable flag on). Legacy rows (source unset / supplied:*) are unchanged."""
from __future__ import annotations

import json
import pathlib

import pytest

from agentic_os.content.search_signals import (
    RELEVANCE_UNKNOWN_AWARE_ENV, SearchObservation, cannibalization, ctr_opportunity, emergent_intent,
    is_not_assessable, missing_page, near_win, relevance_state)
from agentic_os.integrations.gsc import search_signals_from_observations

GOLDEN = json.loads((pathlib.Path(__file__).resolve().parent / "golden" / "search_relevance_golden_base.json").read_text())
INVALID = ["ssr:buyer-score", "SSR:likert", "synthetic:panel", "Synthetic:buyers", "bad source with space", "embedding:",
           "embedding", "unavailable", "unset:x", "embedding:embedding:x", "embedding:" + "x" * 5000, "supplied:ssr-panel",
           "embedding:m‮@1", "еmbedding:m@1", "ｅｍｂｅｄｄｉｎｇ:m@1", "a\x00b", ":x", 5, ["x"]]


def row(src, value=0.95, **kw):
    d = dict(query="agent memory", page_url="https://x.io/blog/p", impressions=500, clicks=2, ctr=0.001, position=8.0,
             relevance=value, days_observed=28, site_id="s", relevance_source=src)
    d.update(kw)
    return SearchObservation(**d)


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(RELEVANCE_UNKNOWN_AWARE_ENV, raising=False)


@pytest.mark.parametrize("src", INVALID, ids=[repr(s)[:30] for s in INVALID])
def test_flag_off_an_invalid_source_is_silent_whatever_its_number(src):
    for value in (0.95, 0.9, 1.0, 0.6):
        o = row(src, value)
        assert relevance_state(o) == "invalid"
        assert near_win(o) is None and missing_page(o) is None
        assert emergent_intent(row(src, value, impressions=20), baseline_impressions=0) is None
    assert not [s for s in search_signals_from_observations([row(src)]) if s.signal_type.value == "NEAR_WIN"]


@pytest.mark.parametrize("src", INVALID, ids=[repr(s)[:30] for s in INVALID])
def test_flag_on_an_invalid_source_is_not_assessable(monkeypatch, src):
    monkeypatch.setenv(RELEVANCE_UNKNOWN_AWARE_ENV, "1")
    for fn in (near_win, missing_page):
        s = fn(row(src))
        assert is_not_assessable(s) and s.status == "WATCH" and s.confidence == 0.0
    assert is_not_assessable(emergent_intent(row(src, impressions=20), baseline_impressions=0))


def test_the_reviewers_case_a_synthetic_labelled_source_can_no_longer_produce_a_near_win():
    assert near_win(row("ssr:buyer-score", 0.95)) is None                       # was: PROPOSE, confidence 0.885


def test_ctr_and_cannibalization_do_not_depend_on_the_source_and_are_unchanged():
    base = ctr_opportunity(row("unset", 0.0, position=3.0, ctr=0.0001))
    assert base is not None
    for src in INVALID[:6]:
        got = ctr_opportunity(row(src, 0.9, position=3.0, ctr=0.0001))
        assert got is not None and got.confidence == base.confidence and got.status == base.status
    pair = [row("ssr:x", 0.9, page_url="a"), row("ssr:x", 0.9, page_url="b")]
    assert [s.signal_type.value for s in cannibalization(pair)] == ["CANNIBALIZATION"]


@pytest.mark.parametrize("src", ["unset", "supplied:crm", "supplied:upstream-v2"])
def test_legacy_rows_are_unchanged_on_the_whole_golden_set(src):
    """No legacy (unset / supplied:*) behaviour changes: every detector row equals the base-commit golden."""
    for i, d in enumerate(GOLDEN["observations"]):
        o = SearchObservation(**dict(d, relevance_source=src))
        base = GOLDEN["per_observation"][i]
        for key, sig in (("near_win", near_win(o)), ("missing_nocanon", missing_page(o)),
                         ("emergent0", emergent_intent(o, baseline_impressions=0))):
            assert (sig is None) == (base[key] is None), (i, key)
            if sig is not None:
                assert sig.confidence == base[key]["c"] and sig.status == base[key]["s"]
