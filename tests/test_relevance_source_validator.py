"""SR-04 round 4: ONE validator for relevance sources (`relevance.parse_relevance_source`), used by RelevanceResult,
apply_relevance, every detector and the evidence renderer. Fail-closed: a source it cannot vouch for is `invalid`
and reads as UNKNOWN downstream, never as a measured value, and no accepted source can contain an ssr/synthetic
token."""
from __future__ import annotations

import random
import re
import unicodedata
from types import SimpleNamespace

import pytest

from agentic_os.content.interventions import content_intervention_queue
from agentic_os.content.relevance import (
    MAX_SOURCE_CHARS, ParsedSource, RelevanceResult, parse_relevance_source)
from agentic_os.content.search_signals import (
    RELEVANCE_UNKNOWN_AWARE_ENV, SearchObservation, is_not_assessable, near_win, relevance_conflicts,
    relevance_reading, relevance_state, relevance_unknown)
from agentic_os.integrations.gsc import apply_relevance, search_signals_from_observations

P = parse_relevance_source
TOKEN = re.compile(r"[a-z0-9]+")


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(RELEVANCE_UNKNOWN_AWARE_ENV, raising=False)


def obs(source, value=0.9, **kw):
    b = dict(query="agentic os", page_url="https://x.io/blog/a", impressions=200, ctr=0.001, position=8.0,
             relevance=value, days_observed=20, site_id="s", relevance_source=source)
    b.update(kw)
    return SearchObservation(**b)


# ── accepted forms, canonicalised ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,kind,text", [
    (None, "unset", "unset"), ("", "unset", "unset"), ("   ", "unset", "unset"), ("UNSET", "unset", "unset"),
    (" Unset ", "unset", "unset"),
    ("embedding:bge-small@r1", "embedding", "embedding:bge-small@r1"),
    ("EMBEDDING:BAAI/bge-small-en-v1.5@probe:abc", "embedding", "embedding:BAAI/bge-small-en-v1.5@probe:abc"),
    ("  Embedding:m@1  ", "embedding", "embedding:m@1"),
    ("unavailable:no_page_text", "unavailable", "unavailable:no_page_text"),
    ("Unavailable:embedder_error:RuntimeError", "unavailable", "unavailable:embedder_error:RuntimeError"),
    ("UNAVAILABLE:x", "unavailable", "unavailable:x"),
    ("supplied:crm", "labelled", "supplied:crm"), ("measured", "labelled", "measured"),
    ("supplied:long-label-for-a-manual-adjustment", "labelled", "supplied:long-label-for-a-manual-adjustment")])
def test_accepted_sources_and_their_canonical_form(raw, kind, text):
    assert P(raw) == ParsedSource(kind, text) and P(raw).valid
    assert P(P(raw).text) == P(raw)                                          # idempotent


# ── rejected forms ────────────────────────────────────────────────────────────────────────────────────────
LONG = "x" * 10_000
REJECTED = [
    ("embedding:" + LONG, "10k embedding"), ("unavailable:" + LONG, "10k unavailable"), ("supplied:" + LONG, "10k label"),
    ("embedding:" + "x" * 400, "400 chars"), (LONG, "10k bare"), ("e" * 301, "301 bare"),
    ("embedding:m@1\x00", "NUL"), ("embed\x00ding:m@1", "NUL inside prefix"), ("embedding:m@1\n", "trailing newline is stripped, so"),
    ("embedding:m\n@1", "newline inside"), ("embedding:m @1", "space inside"), ("embedding:m\t@1", "tab inside"),
    ("embedding:m‮@1", "bidi override"), ("embedding:m⁦@1", "bidi isolate"), ("embedding:m​@1", "zero width"),
    ("embedding:m﻿@1", "BOM"), ("embedding:m­@1", "soft hyphen"), ("embedding:m @1", "line separator"),
    ("embedding:m\U000e0041@1", "tag char"),
    ("ｅｍｂｅｄｄｉｎｇ:m@1", "fullwidth lookalike"), ("еmbedding:m@1", "cyrillic e lookalike"),
    ("embedding：m@1", "fullwidth colon"), ("unavаilable:x", "cyrillic a lookalike"), ("ᴜnavailable:x", "small-cap lookalike"),
    ("embedding", "missing colon"), ("unavailable", "missing colon"), ("EMBEDDING", "missing colon upper"),
    ("embedding:", "empty after prefix"), ("unavailable:", "empty after prefix"), ("supplied:", "empty label"),
    ("embedding:   ", "blank after prefix"), (":x", "empty head"), ("unset:x", "unset with a tail"),
    ("embedding:embedding:m@1", "duplicated prefix"), ("embedding:unavailable:x", "embedding wrapping unavailable"),
    ("unavailable:embedding:m@1", "unavailable wrapping embedding"), ("supplied:embedding:m@1", "labelled wrapping reserved"),
    ("Embedding:EMBEDDING:x", "mixed-case duplicate"), ("unavailable:bad reason", "space in reason"),
    ("unavailable:bad!reason", "punctuation in reason"), ("unavailable:" + "r" * 201, "reason too long"),
    ("ssr:likert", "ssr"), ("SSR", "ssr bare"), ("synthetic:panel", "synthetic"), ("Synthetic:buyers", "synthetic upper"),
    ("embedding:ssr@1", "ssr in embedding"), ("embedding:my_ssr_model@2", "ssr token"), ("embedding:m@synthetic-1", "synthetic rev"),
    ("embedding:ｓｓｒ@1", "fullwidth ssr"), ("supplied:ssr-score", "ssr in label"), ("unavailable:synthetic_panel", "synthetic reason"),
    ("sup plied:x", "space in head"), ("sup/plied:x", "slash in head"), ("supé:x", "non-ascii head"),
    (5, "int"), (b"embedding:x@1", "bytes"), (["embedding:x@1"], "list"), (object(), "object")]


@pytest.mark.parametrize("raw,why", REJECTED, ids=[w for _, w in REJECTED])
def test_rejected_sources_are_invalid_and_never_echoed(raw, why):
    if why.startswith("trailing newline"):
        assert P(raw) == ParsedSource("embedding", "embedding:m@1")        # surrounding whitespace is only trimmed
        return
    parsed = P(raw)
    assert parsed.kind == "invalid" and parsed.text == "" and not parsed.valid


# ── the same verdict everywhere the source is read ───────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,why", REJECTED, ids=[w for _, w in REJECTED])
def test_every_reader_agrees_that_a_rejected_source_is_never_measured(raw, why):
    if why.startswith("trailing newline"):
        return
    # RelevanceResult: neither a value nor an unavailable result can be built from it
    for value in (0.5, None):
        with pytest.raises(ValueError):
            RelevanceResult(value, raw, "m", "r")
    # apply_relevance: a value with that source is refused; a None value never adopts it
    for v in (0.9, 0.0, 0.5):
        o = apply_relevance(obs("unset", 0.0), lambda _o, s=raw, v=v: SimpleNamespace(value=v, source=s))
        assert o.relevance == 0.0 and o.relevance_source == "unavailable:invalid_result"
    o = apply_relevance(obs("unset", 0.0), lambda _o, s=raw: SimpleNamespace(value=None, source=s))
    assert o.relevance_source == "unavailable:no_value" and o.relevance == 0.0
    # detectors: an observation carrying that source reads as unknown/invalid, with the flag on it is not assessable
    row = obs(raw, 0.9)
    assert relevance_state(row) == "invalid" and relevance_unknown(row) and relevance_reading(row)[1] is None
    assert relevance_conflicts([row, obs("embedding:m@1", 0.9), obs("embedding:m@1", 0.1)])[0].evidence[0].startswith(
        "relevance conflict: measured values differ (2 distinct")                    # the invalid row is not a reading


def test_flag_on_a_rejected_source_is_not_assessable_and_its_raw_text_never_reaches_evidence(monkeypatch):
    monkeypatch.setenv(RELEVANCE_UNKNOWN_AWARE_ENV, "1")
    for raw in ("embedding:" + LONG, "embedding:m\n@1‮", "ｅｍｂｅｄｄｉｎｇ:m@1", "ssr:likert\x00" + LONG):
        row = obs(raw, 0.95, relevance_detail="d" * 5000)
        s = near_win(row)
        assert is_not_assessable(s)
        text = "\n".join(s.evidence)
        assert "relevance_source=<invalid>" in text and raw not in text and "x" * 50 not in text
        assert all(len(e) < 900 for e in s.evidence)                               # detail capped at 600
        q = content_intervention_queue([s])
        assert q["count"] == 0 and q["not_assessable"] == 1


def test_flag_off_a_rejected_source_does_not_change_legacy_numeric_decisions():
    # base behaviour for a row judged on its number is preserved (only hook-written unavailable sources override it)
    assert near_win(obs("not a valid source!", 0.8)) is not None and near_win(obs("ssr:x", 0.8)) is not None


# ── the hook only produces valid, whitespace-free sources ────────────────────────────────────────────────
def test_the_hook_builds_valid_sources_even_from_hostile_embedder_names():
    from agentic_os.content.relevance import PageText, make_relevance_fn

    class Emb:
        name = "My Model\nv2 ‮" + "N" * 500
        revision = "rev A\x00" + "R" * 500

        def embed(self, texts):
            return [[1.0, float(len(t) % 7), 0.5] for t in texts]

    class Prov:
        def page_text(self, u):
            return PageText(title="agent memory guide", body_text="x")
    r = make_relevance_fn(Emb(), Prov())(obs("unset", 0.0, query="agent memory"))
    assert r.available and P(r.source).kind == "embedding" and P(r.source).text == r.source
    assert len(r.source) <= MAX_SOURCE_CHARS and " " not in r.source


# ── property tests: no accepted source can carry an ssr/synthetic token ──────────────────────────────────
_PIECES = ["embedding:", "Embedding:", "unavailable:", "UNAVAILABLE:", "unset", "supplied:", "ssr", "SSR", "synthetic",
           "Synthetic", "study", " ", "\t", "\n", "\x00", "​", "‮", "ｓｓｒ", "ｅ", "е", "a", "b", "m@1", "@",
           ":", "-", "_", ".", "/", "x" * 150, "BAAI/bge-small", "no_page_text", "é", "日本", "!", "<", ">", "﻿"]


def _random_sources(n, seed):
    rng = random.Random(seed)
    for _ in range(n):
        yield "".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 7)))


def test_property_no_accepted_source_contains_a_synthetic_token_or_unsafe_character():
    accepted = 0
    for raw in _random_sources(30_000, 20261122):
        p = P(raw)
        if not p.valid:
            assert p.text == "" and p.kind == "invalid"
            continue
        accepted += 1
        text = p.text
        assert len(text) <= MAX_SOURCE_CHARS and text == text.strip()
        assert not any(unicodedata.category(ch)[0] in "CZ" for ch in text)
        folded = unicodedata.normalize("NFKC", text).lower()
        assert not any(t.startswith(("ssr", "synthetic")) for t in TOKEN.findall(folded)), (raw, text)
        assert P(text) == p                                                     # canonical form is a fixed point
        head = text.partition(":")[0]
        assert head.isascii() and re.match(r"^[A-Za-z0-9_.-]+$", head)
        if p.kind in ("embedding", "unavailable"):
            assert text.partition(":")[0] in ("embedding", "unavailable")      # reserved prefix canonical lower case
            assert not text.partition(":")[2].lower().startswith(("embedding:", "unavailable:", "unset:"))
    assert accepted > 500                                                      # the property is exercised, not vacuous


def test_property_apply_relevance_accepts_a_value_exactly_when_the_validator_says_embedding():
    for raw in _random_sources(8_000, 7):
        o = apply_relevance(obs("unset", 0.0), lambda _o, s=raw: SimpleNamespace(value=0.7, source=s))
        kind = P(raw).kind
        if kind == "embedding":
            assert o.relevance == 0.7 and o.relevance_source == P(raw).text
        else:
            assert o.relevance == 0.0 and o.relevance_source == "unavailable:invalid_result"
        assert P(o.relevance_source).valid
