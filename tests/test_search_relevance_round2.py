"""SR-04 round 2: tests written from the independent reviewer's proofs of concept (each failed on round-1 head
d9c9419, see the PR description), covering R1-R6, X1-X4 and M1-M8.

Reviewer artefacts these derive from: harness_bitid.py (bit-identity, see test_search_relevance_bitid.py), probe7
(iterator input), probe8 (duck-typed observations), probe10 (non-Latin conflicts), probe4 (hostile HTML timing),
probe9 (failed probe cached), probe5 (provider/embedder robustness), probe_stuff (keyword stuffing), probe2
(conflict ordering, tolerance, sites), probe1 (source normalisation and invalid values)."""
from __future__ import annotations

import dataclasses
import hashlib
import itertools
import math
import os
import re
import threading
import time
from collections import namedtuple
from types import SimpleNamespace

import pytest

from agentic_os.content import portfolio as P
from agentic_os.content import relevance as R
from agentic_os.content.interventions import content_intervention_queue
from agentic_os.content.relevance import PageText, RelevanceResult, make_relevance_fn, page_text_from_html
from agentic_os.content.search_signals import (
    RELEVANCE_UNKNOWN_AWARE_ENV, SearchObservation, SignalType, cannibalization, ctr_opportunity, emergent_intent,
    is_not_assessable, missing_page, near_win, relevance_conflicts, relevance_reading, relevance_state,
    relevance_unknown)
from agentic_os.integrations.gsc import apply_relevance, search_signals_from_observations

MEM = "https://x.example/memory"
BLOG = "https://x.example/blog/post"


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(RELEVANCE_UNKNOWN_AWARE_ENV, raising=False)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(RELEVANCE_UNKNOWN_AWARE_ENV, "1")


def O(**kw):
    b = dict(query="agentic os", page_url=BLOG, impressions=200, clicks=2, ctr=0.001, position=8.0, relevance=0.0,
             days_observed=20, site_id="s")
    b.update(kw)
    return SearchObservation(**b)


def E(v, m="m@1", **kw):
    kw.setdefault("relevance_detail", "embedder=" + m)
    return O(relevance=v, relevance_source="embedding:" + m, **kw)


def U(**kw):
    return O(relevance_source="unavailable:no_page_text", **kw)


class Bow:
    name = "bow"

    def __init__(self, dim=64):
        self.dim = dim
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in re.findall(r"\w+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
            out.append(v)
        return out


class Prov:
    def __init__(self, pg=None, pages=None):
        self.pg, self.pages, self.calls = pg, pages, []

    def page_text(self, url):
        self.calls.append(url)
        return self.pages.get(url) if self.pages is not None else self.pg


PG = PageText(title="Agent memory guide", body_text="how agents remember things across sessions")


# ── R1: single-use iterables ──────────────────────────────────────────────────────────────────────────────
def _ten_rows():
    return [SearchObservation(query=f"q{i}", page_url=f"https://x.example/p{i % 3}", impressions=100, clicks=1,
                              ctr=0.001, position=8.0, relevance=0.9, days_observed=20, site_id="s") for i in range(6)]


def test_r1_iterator_inputs_give_the_same_per_observation_signals_as_a_list():
    rows = _ten_rows()
    as_list = search_signals_from_observations(rows)
    assert len(as_list) >= 6
    per_obs = [s for s in as_list if s.signal_type is not SignalType.CANNIBALIZATION]
    for wrap in (lambda r: (o for o in r), lambda r: iter(tuple(r)), tuple, list):
        got = search_signals_from_observations(wrap(rows))
        assert [s for s in got if s.signal_type is not SignalType.CANNIBALIZATION] == per_obs
    # base quirk preserved bit-for-bit: a consumed iterator hides cannibalization, a sequence does not
    assert search_signals_from_observations(o for o in rows) == per_obs
    assert search_signals_from_observations(rows) == as_list


def test_r1_iterator_input_with_hook_readings_still_surfaces_conflicts():
    rows = [E(0.9), E(0.2)]
    got = search_signals_from_observations(iter(rows))
    assert [s.signal_type for s in got].count(SignalType.RELEVANCE_CONFLICT) == 1


# ── R2: duck-typed observations ───────────────────────────────────────────────────────────────────────────
_BASE = dict(query="q", page_url="https://x/blog/p", impressions=100, clicks=1, ctr=0.001, position=8.0,
             relevance=0.9, days_observed=20, site_id="s")


def _ducks():
    nt = namedtuple("Obs", _BASE)
    return {"SimpleNamespace": lambda **kw: SimpleNamespace(**{**_BASE, **kw}),
            "namedtuple": lambda **kw: nt(**{**_BASE, **kw})}


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("kind", ["SimpleNamespace", "namedtuple"])
def test_r2_duck_typed_observations_work_in_every_reader(monkeypatch, kind, flag):
    monkeypatch.setenv(RELEVANCE_UNKNOWN_AWARE_ENV, "1") if flag else monkeypatch.delenv(
        RELEVANCE_UNKNOWN_AWARE_ENV, raising=False)
    mk = _ducks()[kind]
    o, ref = mk(), SearchObservation(**_BASE)
    for fn in (lambda x: near_win(x), lambda x: ctr_opportunity(x), lambda x: missing_page(x),
               lambda x: emergent_intent(x, baseline_impressions=0)):
        a, b = fn(o), fn(ref)
        assert (a is None) == (b is None)
        if a is not None:
            assert (a.signal_type, a.status, a.confidence) == (b.signal_type, b.status, b.confidence)
    pair = [mk(), mk(page_url="https://x/other")]
    assert [s.signal_type for s in cannibalization(pair)] == [SignalType.CANNIBALIZATION]
    assert search_signals_from_observations([o]) is not None
    assert relevance_state(o) in ("ok", "low", "unknown", "invalid") and relevance_reading(o)[0] == "unset"
    assert content_intervention_queue(search_signals_from_observations([o, mk(position=3.0, ctr=0.001)])) is not None


def test_r2_duck_typed_equals_dataclass_flag_off():
    for kind, mk in _ducks().items():
        a = search_signals_from_observations([mk(), mk(page_url="https://x/b", position=3.0, ctr=0.0001)])
        b = search_signals_from_observations([SearchObservation(**_BASE),
                                              SearchObservation(**{**_BASE, "page_url": "https://x/b",
                                                                   "position": 3.0, "ctr": 0.0001})])
        assert a == b, kind


# ── R3: conflict key (site, page, exact query) ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("qa,qb", [("цена подписки", "как отменить"), ("料金", "解約方法"), ("!!!", "???"),
                                   ("pricing", "pricing!"), ("café prix", "crème tarif")])
def test_r3_different_queries_never_conflict_and_their_near_wins_are_not_held(qa, qb):
    sigs = search_signals_from_observations([E(0.9, query=qa), E(0.7, query=qb)])
    assert [s for s in sigs if s.signal_type is SignalType.RELEVANCE_CONFLICT] == []
    nw = [s for s in sigs if s.signal_type is SignalType.NEAR_WIN]
    assert len(nw) == 2 and all(s.status == "PROPOSE" for s in nw)


def test_r3_same_query_exact_text_still_conflicts_and_is_order_independent():
    a, b = E(0.9, query="цена"), E(0.2, query="цена")
    results = []
    for perm in itertools.permutations([a, b, U(query="цена")]):
        (c,) = [s for s in search_signals_from_observations(list(perm)) if s.signal_type is SignalType.RELEVANCE_CONFLICT]
        results.append((c.query, c.site_id, c.affected_pages, c.evidence))
    assert len(set(results)) == 1


def test_r3_site_id_is_part_of_the_key_no_cross_tenant_attribution():
    sigs = search_signals_from_observations([E(0.9, site_id="tenantA"), E(0.1, site_id="tenantB")])
    assert [s for s in sigs if s.signal_type is SignalType.RELEVANCE_CONFLICT] == []
    assert all(s.status == "PROPOSE" for s in sigs if s.signal_type is SignalType.NEAR_WIN)
    (c,) = relevance_conflicts([E(0.9, site_id="tenantA"), E(0.1, site_id="tenantA")])
    assert c.site_id == "tenantA"
    assert len(relevance_conflicts([E(0.9, site_id="A", page_url="p1"), E(0.1, site_id="A", page_url="p2")])) == 0


# ── R4: hostile HTML must be fast and never raise ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,html", [
    ("a_x", "<a>x" * 150_000), ("unmatched_end", "<a>" * 100_000 + "</b>" * 100_000),
    ("deep_div", "<div>" * 200_000 + "hi"), ("lt", "<" * 900_000), ("comment", "<!--" + "x" * 900_000),
    ("hidden_deep", "<span hidden>" * 100_000 + "x" * 1000), ("p_x", "<p>x</p>" * 100_000),
    ("charrefs", "&#x41;" * 150_000), ("title_tags", "<title>" + "<b>x</b>" * 50_000),
    ("h1_tags", "<h1>" + "<i>y</i>" * 50_000), ("attrs", "<a " + "b=c " * 100_000 + ">x")])
def test_r4_hostile_html_is_linear_and_bounded(name, html):
    t0 = time.perf_counter()
    p = page_text_from_html(html)
    assert time.perf_counter() - t0 < 1.0, name
    assert isinstance(p, PageText) and len(p.body_text) <= 2 * 100_000 + 10


def test_r4_never_raises_and_still_extracts_normal_pages():
    for junk in (None, 5, b"x", "", "<", "</", "<a", "<!DOCTYPE", "&", "<<>>", "\x00" * 10, "<a b='" + "x" * 500):
        assert isinstance(page_text_from_html(junk), PageText)
    p = page_text_from_html("<html><head><title>T</title><meta name=description content='M'></head><body>"
                            "<h1>Head</h1><p>vis</p><p hidden>no</p></body></html>")
    assert (p.title, p.h1, p.meta_description) == ("T", "Head", "M") and "vis" in p.body_text and "no" not in p.body_text
    assert R._MAX_HTML_CHARS <= 200_000


# ── R5: unset with a non-zero value is supplied-unlabelled (measured) ─────────────────────────────────────
def test_r5_flag_on_unlabelled_nonzero_relevance_behaves_as_today(on):
    legacy = O(relevance=0.8)                                  # an existing integration: value, default source
    s = near_win(legacy)
    assert s is not None and s.status == "PROPOSE" and not is_not_assessable(s)
    assert any("supplied, unlabelled" in e and "supplied:<label>" in e for e in s.evidence)
    assert relevance_state(legacy) == "ok" and not relevance_unknown(legacy)
    assert near_win(O(relevance=0.2)) is None                  # unlabelled but low: silent, as today
    assert relevance_state(O(relevance=0.2)) == "low"
    unknown = near_win(O(relevance=0.0))                       # unset AND 0.0 is the legacy default: unknown
    assert is_not_assessable(unknown) and relevance_state(O()) == "unknown"
    assert missing_page(O(relevance=0.8)).status == "PROPOSE"
    assert emergent_intent(O(relevance=0.95, impressions=20), baseline_impressions=0).status == "WATCH"
    assert not is_not_assessable(emergent_intent(O(relevance=0.95, impressions=20), baseline_impressions=0))


# ── R6: the source is authoritative; legacy rows stay out of conflict logic ───────────────────────────────
@pytest.mark.parametrize("flag", [False, True])
def test_r6_unavailable_source_ignores_its_placeholder_number(monkeypatch, flag):
    monkeypatch.setenv(RELEVANCE_UNKNOWN_AWARE_ENV, "1") if flag else monkeypatch.delenv(
        RELEVANCE_UNKNOWN_AWARE_ENV, raising=False)
    poisoned = O(relevance=0.9, relevance_source="unavailable:x")
    assert relevance_state(poisoned) == "unknown" and relevance_reading(poisoned)[1] is None
    for fn in (near_win, missing_page, lambda o: emergent_intent(O(**{**o.__dict__, "impressions": 20}),
                                                               baseline_impressions=0)):
        s = fn(poisoned)
        if flag:
            assert s is not None and is_not_assessable(s)
            assert not any(re.search(r"relevance 0\.\d\d", e) for e in s.evidence)      # no self-contradiction
        else:
            assert s is None
    assert not [s for s in search_signals_from_observations([poisoned]) if s.signal_type is SignalType.NEAR_WIN
                and not is_not_assessable(s)]


def test_r6_legacy_unset_rows_are_not_pulled_into_conflicts(monkeypatch):
    legacy, hook = O(relevance=0.8), E(0.8)
    for flag in (False, True):
        monkeypatch.setenv(RELEVANCE_UNKNOWN_AWARE_ENV, "1") if flag else monkeypatch.delenv(
            RELEVANCE_UNKNOWN_AWARE_ENV, raising=False)
        sigs = search_signals_from_observations([legacy, E(0.1)])
        assert [s for s in sigs if s.signal_type is SignalType.RELEVANCE_CONFLICT] == []
        nw = [s for s in sigs if s.signal_type is SignalType.NEAR_WIN]
        assert len(nw) == 1 and nw[0].status == "PROPOSE"            # the legacy row is judged as it always was
        assert relevance_conflicts([legacy, U(), E(0.9)])[0].evidence[0].startswith("relevance conflict")
        assert not any(legacy.relevance_source in e for s in relevance_conflicts([legacy, U(), E(0.9)])
                       for e in s.evidence if e.startswith("reading: unset"))


# ── M1-M4: tolerance, order, equal-values, invalid values ─────────────────────────────────────────────────
def test_m1_float_jitter_is_not_a_conflict_a_real_difference_is():
    assert relevance_conflicts([E(0.7), E(0.7 + 1e-12)]) == []
    assert relevance_conflicts([E(0.7), E(0.7 + 1e-7)]) != []


def test_m2_conflict_signal_is_order_independent_including_attribution():
    obs = [E(0.9, "mA@1"), E(0.2, "mB@2"), U(), E(0.5, "mA@1")]
    seen = {tuple((s.query, s.site_id, s.affected_pages, s.evidence) for s in relevance_conflicts(list(p)))
            for p in itertools.permutations(obs)}
    assert len(seen) == 1


def test_m3_equal_values_from_different_embedders_are_not_a_conflict_but_are_listed_if_another_conflicts():
    assert relevance_conflicts([E(0.7, "a@1"), E(0.7, "b@2")]) == []
    (c,) = relevance_conflicts([E(0.7, "a@1"), E(0.7, "b@2"), E(0.1, "a@1")])
    assert "embedding:a@1" in " ".join(c.evidence) and "embedding:b@2" in " ".join(c.evidence)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 1.5, -0.2])
def test_m4_invalid_values_are_invalid_and_unknown_to_the_aware_detectors(on, bad):
    for src in ("embedding:m@1", "supplied:x"):
        o = O(relevance=bad, relevance_source=src)
        assert relevance_state(o) == "invalid" and relevance_unknown(o)
        assert is_not_assessable(near_win(o))
        assert relevance_reading(o)[1] is None
    mixed = [O(relevance=bad, relevance_source="supplied:x"), O(relevance=0.9, relevance_source="supplied:x")]
    (c,) = relevance_conflicts(mixed + [O(relevance=float("nan"), relevance_source="supplied:x")])
    assert "measured and unknown" in c.evidence[0]


def test_m4_flag_off_legacy_numeric_behaviour_is_untouched_for_invalid_numbers():
    assert near_win(O(relevance=7.0)) is not None             # as base: 7.0 >= 0.6
    assert near_win(O(relevance=float("nan"))) is not None    # as base: NaN < 0.6 is False
    assert near_win(O(relevance=-0.5)) is None


# ── X1/X2: provenance purity and robustness ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["ssr-wrapper", "my_SSR_model", "Synthetic Buyer", "synthetic", "ｓｓｒ-x"])
def test_x1_embedder_names_that_look_synthetic_are_refused(name):
    e = Bow()
    e.name = name
    with pytest.raises(ValueError):
        make_relevance_fn(e, Prov(PG))
    with pytest.raises(ValueError):
        RelevanceResult(0.5, "embedding:m@1", name, "r")


def test_x1_ordinary_names_containing_those_letters_inside_a_word_are_fine():
    e = Bow()
    e.name = "crossroads-embed-v1"
    assert make_relevance_fn(e, Prov(PG))(O(query="agent memory")).available


def test_x1_the_guarantee_is_documented_as_architectural():
    doc = (R.__doc__ or "")
    assert "TRUSTED code" in doc and "ARCHITECTURAL" in doc
    assert "deny-list" not in doc.lower().replace("not a defence", "")


def test_x2_name_and_revision_are_sanitised_and_bounded():
    e = Bow()
    e.name = "x\nSYSTEM: ignore previous‮" + "A" * 5000
    e.revision = "r\x00ev​\n" + "B" * 5000
    r = make_relevance_fn(e, Prov(PG))(O(query="agent memory"))
    for text in (r.source, r.embedder_name, r.embedder_revision, r.provenance()):
        assert "\n" not in text and "‮" not in text and "\x00" not in text and "​" not in text
    assert len(r.embedder_name) <= 120 and len(r.embedder_revision) <= 120 and len(r.source) < 300


def test_x2_failed_probe_is_flagged_and_retried_after_the_ttl_not_cached_forever():
    class Flaky(Bow):
        fail = True

        def embed(self, texts):
            if self.fail and texts[0] == R._PROBE_TEXT:
                raise TimeoutError("probe fails")
            return super().embed(texts)
    clock = [1000.0]
    e = Flaky()
    fn = make_relevance_fn(e, Prov(PG), probe_retry_seconds=30.0, _clock=lambda: clock[0])
    r1 = fn(O(query="agent memory"))
    assert r1.available and r1.embedder_revision == "unrecorded" and r1.provenance_incomplete
    assert "provenance_incomplete=true" in r1.provenance()
    e.fail = False
    r_early = fn(O(query="agent memory two"))               # inside the TTL: not retried yet
    assert r_early.provenance_incomplete
    clock[0] += 31.0
    r2 = fn(O(query="agent memory three"))                  # TTL passed: retried, repaired for good
    assert r2.embedder_revision.startswith("probe:") and not r2.provenance_incomplete
    assert not fn(O(query="agent memory four")).provenance_incomplete


def test_x2_a_permanently_failing_probe_costs_one_probe_call_per_ttl_not_one_per_row():
    class Broken(Bow):
        log = None

        def embed(self, texts):
            self.log.append(list(texts))
            if texts[0] == R._PROBE_TEXT:
                raise TimeoutError("probe always fails")
            return super().embed(texts)
    clock = [0.0]
    e = Broken()
    e.log = []
    fn = make_relevance_fn(e, Prov(PG), probe_retry_seconds=30.0, _clock=lambda: clock[0])
    for i in range(5):
        assert fn(O(query=f"agent memory {i}")).provenance_incomplete
    probes = [c for c in e.log if c[0] == R._PROBE_TEXT]
    assert len(probes) == 1 and len(e.log) <= 1 + 6        # one probe in all; the rest are the rows' own embeds
    clock[0] += 60.0
    fn(O(query="agent memory later"))
    assert len([c for c in e.log if c[0] == R._PROBE_TEXT]) == 2            # bounded retry after the TTL


@pytest.mark.parametrize("src", ["Unavailable:x", "UNAVAILABLE:no_page_text", " unavailable:x", "UnAvailable:x"])
def test_x2_source_casing_and_whitespace_never_turn_unknown_into_measured_low(on, src):
    o = O(relevance=0.0, relevance_source=src)
    assert relevance_state(o) == "unknown" and is_not_assessable(near_win(o))
    assert relevance_state(O(relevance=0.9, relevance_source=src)) == "unknown"
    for unset in ("UNSET", " unset", "Unset ", "", None):
        assert relevance_state(O(relevance_source=unset)) == "unknown"


def test_x2_relevance_result_and_apply_relevance_canonicalise_sources():
    r = RelevanceResult(0.5, " Embedding:m@1 ", "m", "1")
    assert r.source == "embedding:m@1"
    assert RelevanceResult(None, "UNAVAILABLE:x").source == "unavailable:x"
    o = apply_relevance(O(), lambda _o: SimpleNamespace(value=0.5, source="EMBEDDING:m@1", provenance=lambda: "d"))
    assert o.relevance_source == "embedding:m@1" and o.relevance == 0.5
    o = apply_relevance(O(), lambda _o: SimpleNamespace(value=None, source="Unavailable:no_page_text"))
    assert o.relevance_source == "unavailable:no_page_text"
    for src in ("Ssr:x", "ＳＳＲ", "SYNTHETIC:y"):
        assert apply_relevance(O(), lambda _o, s=src: SimpleNamespace(value=0.9, source=s)
                               ).relevance_source == "unavailable:invalid_result"


def test_x2_constant_embedder_is_reported_as_degenerate():
    class Const(Bow):
        def embed(self, texts):
            return [[1.0, 0.0, 0.0] for _ in texts]
    r = make_relevance_fn(Const(), Prov(PG))(O(query="anything at all"))
    assert r.value is None and r.source == "unavailable:degenerate_embedder"


# ── X3: thread safety and provider-call dedup ─────────────────────────────────────────────────────────────
def test_x3_provider_is_called_once_per_page_and_cache_is_thread_safe():
    pages = {f"https://x/p{i}": PageText(title=f"title {i} agent memory", body_text="x " * i) for i in range(40)}
    prov = Prov(pages=pages)
    fn = make_relevance_fn(Bow(), prov, cache_size=8)
    errs, out = [], {}

    def worker(k):
        for i in range(120):
            r = fn(O(query=f"agent memory {i % 5}", page_url=f"https://x/p{(i * 3 + k) % 40}"))
            if not r.available:
                errs.append(r.source)
            out[(k, i)] = r.value
    ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errs == []
    solo = make_relevance_fn(Bow(), Prov(pages=pages))
    assert all(out[(k, i)] == solo(O(query=f"agent memory {i % 5}", page_url=f"https://x/p{(i * 3 + k) % 40}")).value
               for k in range(8) for i in range(0, 120, 17))
    plain = Prov(pages=pages)
    f2 = make_relevance_fn(Bow(), plain)
    for _ in range(30):
        f2(O(query="agent memory", page_url="https://x/p1"))
    assert plain.calls == ["https://x/p1"]


# ── X4: stuffing mitigation ───────────────────────────────────────────────────────────────────────────────
def test_x4_repeated_runs_and_duplicated_text_collapse():
    assert R.collapse_repeats("buy now " * 50).split() == ["buy", "now", "buy", "now"]
    assert R.collapse_repeats("agent agent agent agent agent agent") == "agent agent agent"
    base = "kubernetes cluster backup restore a single namespace snapshot"
    assert R.collapse_repeats(base + " " + base + " " + base).split()[:8] == base.split()
    assert len(R.collapse_repeats(base + " " + base + " " + base).split()) <= 8 + 2
    assert len(R.collapse_repeats(base + " " + base).split()) <= len(base.split()) + 2
    honest = "Agent memory lets an agent remember facts across sessions."
    assert R.collapse_repeats(honest) == honest
    assert R.collapse_repeats("") == "" and R.collapse_repeats("   ") == ""


def test_x4_stuffed_body_does_not_outscore_one_copy_with_the_bag_of_words_fake():
    once = PageText(title="Notes", body_text="agent memory for ai agents")
    stuffed = PageText(title="Notes", body_text="agent memory for ai agents " * 400)
    q = O(query="agent memory for ai agents")
    a = make_relevance_fn(Bow(), Prov(once))(q)
    b = make_relevance_fn(Bow(), Prov(stuffed))(q)
    assert abs(a.value - b.value) < 0.05


# ── M5: portfolio rollup ──────────────────────────────────────────────────────────────────────────────────
def test_m5_portfolio_rollup_reports_unknown_relevance_only_when_present(monkeypatch):
    def fake(queue_extra):
        def scan_site(client, site, **kw):
            q = {"count": 0, "requires_approval": 0, "auto": 0, "watching": 1, "decisions": []}
            q.update(queue_extra.get(site.site_id, {}))
            return {"site": site.site_id, "domain": site.domain, "pages": 1, "resolved": True, "queue": q}
        return scan_site
    sites = [P.Site(site_id="a", domain="a.example"), P.Site(site_id="b", domain="b.example")]
    monkeypatch.setattr(P, "scan_site", fake({}))
    plain = P.scan_portfolio(None, sites)
    assert "total_not_assessable" not in plain and "total_relevance_conflicts" not in plain
    monkeypatch.setattr(P, "scan_site", fake({"a": {"not_assessable": 2}, "b": {"not_assessable": 1,
                                                                                  "relevance_conflicts": 3}}))
    rolled = P.scan_portfolio(None, sites)
    assert rolled["total_not_assessable"] == 3 and rolled["total_relevance_conflicts"] == 3
    assert {k: v for k, v in rolled.items() if not k.startswith("total_not") and k != "total_relevance_conflicts"} == plain


# ── M6: hostile result objects ────────────────────────────────────────────────────────────────────────────
def test_m6_apply_relevance_survives_a_result_object_whose_properties_raise():
    class Evil:
        @property
        def value(self):
            raise RuntimeError("boom")

        source = "embedding:m@1"

    class Evil2:
        value = 0.5

        @property
        def source(self):
            raise ZeroDivisionError

    class Evil3:
        value, source = 0.5, "embedding:m@1"

        def provenance(self):
            raise KeyError("x")
    for res in (Evil(), Evil2(), Evil3()):
        o = apply_relevance(O(), lambda _o, r=res: r)
        assert o.relevance == 0.0 and o.relevance_source.startswith("unavailable:")


# ── M7/M8: evidence text hygiene ──────────────────────────────────────────────────────────────────────────
def test_m8_no_evidence_line_ends_with_a_dangling_separator(on):
    sigs = [near_win(E(0.9, relevance_detail="")), near_win(O(relevance_source="unavailable:x")),
            ctr_opportunity(E(0.5, relevance_detail="")), ctr_opportunity(O(relevance_source="unavailable:x"))]
    sigs += cannibalization([E(0.9, page_url="p1", relevance_detail=""), U(page_url="p2", relevance_detail="")])
    sigs += relevance_conflicts([E(0.9, relevance_detail=""), U(relevance_detail="")])
    for s in sigs:
        assert s is not None
        for e in s.evidence:
            assert not e.rstrip().endswith(";") and "; ;" not in e, e


def test_m7_docs_do_not_claim_unchanged_where_evidence_lines_are_added():
    text = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "SEARCH_RELEVANCE.md")).read()
    assert "are untouched" not in text and "and are untouched" not in text.lower()


def test_r2_no_reader_in_the_package_reads_the_new_fields_by_attribute_access():
    """Every read of relevance_source / relevance_detail goes through getattr (duck-typed observations that predate
    the fields must keep working); the fields are only ever WRITTEN via dataclasses.replace / construction."""
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in list((root / "agentic_os").rglob("*.py")) + list((root / "apps").glob("*/*.py")):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr in ("relevance_source", "relevance_detail")
                    and isinstance(node.ctx, ast.Load)):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []


def test_r6_a_legacy_row_for_a_conflicted_pair_is_judged_as_it_always_was():
    legacy = O(relevance=0.8)                                   # source 'unset', value 0.8: PROPOSE today
    for flag in (False, True):
        sigs = search_signals_from_observations([legacy, E(0.9), E(0.1)])
        assert len([s for s in sigs if s.signal_type is SignalType.RELEVANCE_CONFLICT]) == 1
        nw = [s for s in sigs if s.signal_type is SignalType.NEAR_WIN]
        by_ev = {("provenance" in " ".join(s.evidence)): s for s in nw}
        legacy_sig = [s for s in nw if not any("relevance provenance" in e for e in s.evidence)]
        hooked_sig = [s for s in nw if any("relevance provenance" in e for e in s.evidence)]
        assert len(legacy_sig) == 1 and legacy_sig[0].status == "PROPOSE"       # NOT held: legacy rows never are
        assert len(hooked_sig) == 1 and hooked_sig[0].status == "WATCH"         # the hook's 0.9 is held
        assert by_ev


# ── round 3: apply_relevance must reject foreign / synthetic-looking sources whatever their length ─────────────
@pytest.mark.parametrize("src", [
    "embedding:ssr@1", "embedding:synthetic-panel@2", "Embedding:SSR-buyer@1", "embedding:my_ssr_model@3",
    "supplied:long-label-for-a-manually-adjusted-relevance-value", "supplied:" + "x" * 80,
    "ssr:" + "purchase-intent-likert-pmf-mean" * 3, "synthetic:panel", "synthetic:panel-of-twelve-buyers",
    "human:crowd-rated-relevance-score-v2", "measured", "unset", "", "unavailable:with-a-value-is-invalid",
    "embedding:"])
def test_round3_apply_relevance_refuses_long_foreign_and_synthetic_looking_sources(src):
    for value in (0.9, 0.0, 0.5):
        o = apply_relevance(O(), lambda _o, s=src, v=value: SimpleNamespace(value=v, source=s, provenance=lambda: "d"))
        assert o.relevance == 0.0 and o.relevance_source == "unavailable:invalid_result", (src, value)


def test_round3_apply_relevance_still_accepts_a_real_embedding_source():
    o = apply_relevance(O(), lambda _o: SimpleNamespace(value=0.7, source="embedding:BAAI/bge-small-en-v1.5@probe:abc",
                                                        provenance=lambda: "embedder=x"))
    assert o.relevance == 0.7 and o.relevance_source == "embedding:BAAI/bge-small-en-v1.5@probe:abc"
    o = apply_relevance(O(), lambda _o: SimpleNamespace(value=0.7, source="embedding:crossroads-embed@1"))
    assert o.relevance_source.startswith("embedding:crossroads")


def test_round3_docs_do_not_overstate_thread_safety_or_the_ledger_guard():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "SEARCH_RELEVANCE.md").read_text()
    low = text.lower()
    assert "thread-safe" not in low and "one provider call per page" not in low
    assert "real sources and refs are unaffected" not in low and "can never enter" not in low
    assert "naming-convention" in low and "single-flight" in low
