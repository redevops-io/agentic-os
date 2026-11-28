"""SR-04 round 4: relevance is NEVER silently turned into 0.0 (measured low).

Every code path where relevance could be defaulted or coerced to zero is enumerated here and asserted to produce an
explicit unknown / unassessable state WITH A REASON, and no downstream reader treats it as measured-low."""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import pathlib
import re
import threading
import time
from types import SimpleNamespace

import pytest

from agentic_os.content import relevance as R
from agentic_os.content.interventions import content_intervention_queue
from agentic_os.content.relevance import PageText, make_relevance_fn
from agentic_os.content.search_signals import (
    RELEVANCE_UNKNOWN_AWARE_ENV, SearchObservation, cannibalization, emergent_intent, is_not_assessable, missing_page,
    near_win, relevance_reading, relevance_state, relevance_unknown)
from agentic_os.integrations.gsc import GscClient, apply_relevance, collect_search_observations

ROOT = pathlib.Path(__file__).resolve().parent.parent
URL = "https://x.io/blog/memory"
PG = PageText(title="Agent memory guide", body_text="how agents remember things across sessions")


class Stub(GscClient):
    def __init__(self, rows):
        super().__init__(key_file="/dev/null")
        self.rows = rows

    def _bearer(self):
        return "t"

    def search_analytics(self, site_url, *, days=28, row_limit=1000, dimensions=("query", "page")):
        return self.rows


ROW = {"keys": ["agent memory", URL], "clicks": 9, "impressions": 300, "ctr": 0.03, "position": 8.0}


class Emb:
    name = "bow"

    def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * 64
            for w in re.findall(r"\w+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
            out.append(v)
        return out


class Prov:
    def __init__(self, pg=PG):
        self.pg = pg

    def page_text(self, u):
        return self.pg


def collect(fn):
    return collect_search_observations(Stub([ROW]), "https://x.io/", relevance_fn=fn)[0]


def failing_embedder(exc):
    class E(Emb):
        def embed(self, texts):
            if texts[0] == R._PROBE_TEXT:
                return super().embed(texts)
            raise exc
    return E()


class Hang(Emb):
    def __init__(self):
        self.release = threading.Event()

    def embed(self, texts):
        if texts[0] != R._PROBE_TEXT:
            self.release.wait(5)
        return super().embed(texts)


class Const(Emb):
    def embed(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


class Zero(Emb):
    def embed(self, texts):
        return [[0.0, 0.0] for _ in texts]


class Nan(Emb):
    def embed(self, texts):
        return [[float("nan"), 1.0] for _ in texts]


class Wrong(Emb):
    def embed(self, texts):
        return []


def boom(_o):
    raise RuntimeError("hook failure")


# (name, relevance_fn builder, expected source reason prefix)
PATHS = [
    ("hook function raises", lambda: boom, "unavailable:relevance_fn_error:RuntimeError"),
    ("hook returns None", lambda: (lambda o: None), "unavailable:no_value"),
    ("hook returns garbage", lambda: (lambda o: object()), "unavailable:no_value"),
    ("hook returns an out-of-range value", lambda: (lambda o: SimpleNamespace(value=7.0, source="embedding:m@1")),
     "unavailable:invalid_result"),
    ("hook returns a 0-100 scale value", lambda: (lambda o: SimpleNamespace(value=85, source="embedding:m@1")),
     "unavailable:invalid_result"),
    ("hook returns a negative value", lambda: (lambda o: SimpleNamespace(value=-0.1, source="embedding:m@1")),
     "unavailable:invalid_result"),
    ("hook returns NaN", lambda: (lambda o: SimpleNamespace(value=float("nan"), source="embedding:m@1")),
     "unavailable:invalid_result"),
    ("hook returns inf", lambda: (lambda o: SimpleNamespace(value=float("inf"), source="embedding:m@1")),
     "unavailable:invalid_result"),
    ("hook returns a value with a bad source", lambda: (lambda o: SimpleNamespace(value=0.9, source="supplied:x")),
     "unavailable:invalid_result"),
    ("page provider raises", lambda: make_relevance_fn(Emb(), SimpleNamespace(
        page_text=lambda u: (_ for _ in ()).throw(OSError("disk")))), "unavailable:page_text_provider_error:OSError"),
    ("page provider returns None", lambda: make_relevance_fn(Emb(), Prov(None)), "unavailable:no_page_text"),
    ("page provider returns empty text", lambda: make_relevance_fn(Emb(), Prov(PageText())), "unavailable:no_page_text"),
    ("page provider returns only invisible characters",
     lambda: make_relevance_fn(Emb(), Prov(PageText(title="​‮", body_text="\x00"))), "unavailable:no_page_text"),
    ("embedder raises", lambda: make_relevance_fn(failing_embedder(RuntimeError("x")), Prov()),
     "unavailable:embedder_error:RuntimeError"),
    ("embedder returns the wrong number of vectors", lambda: make_relevance_fn(Wrong(), Prov()),
     "unavailable:embedder_error:bad_shape"),
    ("embedder returns zero vectors", lambda: make_relevance_fn(Zero(), Prov()), "unavailable:degenerate_embedding"),
    ("embedder returns NaN vectors", lambda: make_relevance_fn(Nan(), Prov()), "unavailable:degenerate_embedding"),
    ("embedder is constant (degenerate)", lambda: make_relevance_fn(Const(), Prov()), "unavailable:degenerate_embedder"),
    ("embedder call times out", lambda: make_relevance_fn(Hang(), Prov(), call_timeout_seconds=0.2),
     "unavailable:embedder_timeout"),
    ("page provider call times out", lambda: make_relevance_fn(Emb(), SimpleNamespace(
        page_text=lambda u: time.sleep(1.5)), call_timeout_seconds=0.2), "unavailable:page_text_provider_timeout"),
]


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(RELEVANCE_UNKNOWN_AWARE_ENV, raising=False)


@pytest.mark.parametrize("name,build,reason", PATHS, ids=[p[0] for p in PATHS])
def test_every_failure_path_yields_an_explicit_unknown_with_a_reason(name, build, reason):
    o = collect(build())
    assert o.relevance_source == reason and o.relevance == 0.0           # 0.0 is only the placeholder ...
    assert relevance_state(o) == "unknown" and relevance_unknown(o)       # ... the state is unknown, with a reason
    assert relevance_reading(o)[1] is None and o.relevance_source.split(":", 1)[1]
    assert o != dataclasses.replace(o, relevance_source="embedding:m@1")  # distinguishable from a measured 0.0


@pytest.mark.parametrize("name,build,reason", PATHS, ids=[p[0] for p in PATHS])
def test_no_downstream_reader_treats_a_failed_path_as_measured_low(monkeypatch, name, build, reason):
    o = collect(build())
    # flag off: silent exactly as unknown always was (and not a 'low' verdict anywhere)
    assert near_win(o) is None and missing_page(dataclasses.replace(o, page_url="https://x.io/blog/p")) is None
    monkeypatch.setenv(RELEVANCE_UNKNOWN_AWARE_ENV, "1")
    nw = near_win(o)
    assert is_not_assessable(nw) and f"relevance_source={reason}" in nw.evidence[1]
    mp = missing_page(dataclasses.replace(o, page_url="https://x.io/blog/p"))
    assert is_not_assessable(mp)
    assert is_not_assessable(emergent_intent(dataclasses.replace(o, impressions=20), baseline_impressions=0))
    q = content_intervention_queue([nw, mp])
    assert q["count"] == 0 and q["not_assessable"] == 2
    lines = [e for s in cannibalization([o, dataclasses.replace(o, page_url="https://x.io/other")]) for e in s.evidence]
    assert any("unknown, not measured" in e for e in lines) and not any("value=0.000" in e for e in lines)


def test_the_cache_hit_of_a_failure_is_still_an_explicit_unknown():
    clock = [0.0]
    fn = make_relevance_fn(failing_embedder(RuntimeError("x")), Prov(), failure_ttl_seconds=30, _clock=lambda: clock[0])
    first, second = collect(fn), collect(fn)
    assert first.relevance_source == second.relevance_source == "unavailable:embedder_error:RuntimeError"
    assert relevance_state(second) == "unknown"
    fn2 = make_relevance_fn(Emb(), Prov(None), failure_ttl_seconds=30)
    assert collect(fn2).relevance_source == collect(fn2).relevance_source == "unavailable:no_page_text"


def test_a_failed_probe_is_flagged_incomplete_not_zero_and_a_missing_revision_is_never_invented():
    class BadProbe(Emb):
        def embed(self, texts):
            if texts[0] == R._PROBE_TEXT:
                raise TimeoutError("probe")
            return super().embed(texts)
    r = make_relevance_fn(BadProbe(), Prov())(SearchObservation(query="agent memory", page_url=URL))
    assert r.available and r.provenance_incomplete and r.embedder_revision == "unrecorded" and r.value > 0.0


def test_missing_relevance_and_the_legacy_default_read_as_unknown_not_low():
    for row in (SearchObservation(query="q", page_url=URL), SearchObservation(query="q", page_url=URL, relevance=0.0),
                SearchObservation(query="q", page_url=URL, relevance_source=None), SimpleNamespace(
                    query="q", page_url=URL, impressions=1, clicks=0, ctr=0.0, position=1.0, relevance=0.0,
                    days_observed=1, site_id="")):
        assert relevance_state(row) == "unknown" and relevance_reading(row)[1] is None


@pytest.mark.parametrize("value", [85, 100, 100.0, 1.0000001, 7.0, -0.5, -1, float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("source", ["unset", "supplied:crm", "embedding:m@1"])
def test_unsupported_scales_are_invalid_not_coerced_to_zero_or_clamped(monkeypatch, value, source):
    o = SearchObservation(query="q", page_url=URL, impressions=300, position=8.0, days_observed=28, relevance=value,
                          relevance_source=source)
    assert relevance_state(o) == "invalid" and relevance_unknown(o) and relevance_reading(o)[1] is None
    monkeypatch.setenv(RELEVANCE_UNKNOWN_AWARE_ENV, "1")
    assert is_not_assessable(near_win(o))
    assert o.relevance != 0.0 or value == 0.0                           # the stored number is not rewritten


def test_flag_off_legacy_numeric_behaviour_for_unsupported_scales_is_unchanged():
    o = lambda v: SearchObservation(query="q", page_url=URL, impressions=300, position=8.0, days_observed=28,    # noqa: E731
                                    relevance=v)
    assert near_win(o(85)) is not None and near_win(o(7.0)) is not None and near_win(o(-0.5)) is None


def test_no_code_in_the_package_assigns_a_literal_zero_relevance_to_a_result_or_a_replacement():
    """Static: nowhere in agentic_os is a relevance of 0 / 0.0 constructed explicitly for a hook result,
    RelevanceResult or dataclasses.replace(...) (the only 0.0 is the dataclass default placeholder)."""
    offenders = []
    for path in (ROOT / "agentic_os").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = getattr(node.func, "id", None) or getattr(node.func, "attr", "")
                for kw in node.keywords:
                    if (kw.arg == "relevance" and isinstance(kw.value, ast.Constant) and kw.value.value in (0, 0.0)
                            and fn in ("RelevanceResult", "replace", "SearchObservation", "apply_relevance")):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                if fn == "RelevanceResult" and node.args and isinstance(node.args[0], ast.Constant) \
                        and node.args[0].value in (0, 0.0):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []
