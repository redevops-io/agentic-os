"""SR-04 round 4: single-flight, bounded failure caching and bounded waits for the relevance hook, with REAL threads.

N=16 concurrent identical requests share one provider call and one embed call per distinct text; waiters receive the
leader's result or the same failure; a hung dependency blocks nobody forever (bounded wait) and never blocks another
key; failures are cached briefly with a capped TTL (injectable clock) and the caches stay bounded."""
from __future__ import annotations

import hashlib
import re
import threading
import time

import pytest

from agentic_os.content import relevance as R
from agentic_os.content.relevance import PageText, make_relevance_fn
from agentic_os.content.search_signals import SearchObservation

N = 16
PG = PageText(title="Agent memory guide", body_text="how agents remember things across sessions")


def obs(query="agent memory", url="https://x/p"):
    return SearchObservation(query=query, page_url=url, impressions=100, position=8.0, days_observed=20)


class Emb:
    """Deterministic bag-of-words embedder that counts calls per text and can delay, fail or hang."""
    name = "bow"

    def __init__(self, delay=0.0):
        self.delay, self.lock, self.calls = delay, threading.Lock(), []
        self.fail_with = None
        self.hang_on = None               # substring: embedding a text containing it blocks until released
        self.release = threading.Event()
        self.started = threading.Event()

    def embed(self, texts):
        with self.lock:
            self.calls.append(list(texts))
        if self.hang_on and any(self.hang_on in t for t in texts):
            self.started.set()
            self.release.wait(10)
        if self.delay:
            time.sleep(self.delay)
        if self.fail_with is not None and texts[0] not in (R._PROBE_TEXT,):
            raise self.fail_with
        out = []
        for t in texts:
            v = [0.0] * 64
            for w in re.findall(r"\w+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
            out.append(v)
        return out

    def count(self, text_part):
        with self.lock:
            return sum(1 for c in self.calls for t in c if text_part in t)


class Prov:
    def __init__(self, delay=0.0, pages=None):
        self.delay, self.lock, self.calls = delay, threading.Lock(), []
        self.pages = pages
        self.fail_with = None
        self.hang_on = None
        self.release = threading.Event()

    def page_text(self, url):
        with self.lock:
            self.calls.append(url)
        if self.hang_on and self.hang_on in url:
            self.release.wait(10)
        if self.delay:
            time.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        return PG if self.pages is None else self.pages.get(url)

    def count(self, url):
        with self.lock:
            return sum(1 for c in self.calls if c == url)


def run_threads(fn, n=N):
    barrier = threading.Barrier(n)
    out = [None] * n
    errs = []

    def worker(i):
        try:
            barrier.wait(5)
            out[i] = fn(i)
        except BaseException as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    t0 = time.perf_counter()
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert not any(t.is_alive() for t in ts), "deadlock"
    assert errs == []
    return out, time.perf_counter() - t0


# ── identical concurrent requests ────────────────────────────────────────────────────────────────────────
def test_16_concurrent_identical_requests_make_one_provider_call_and_one_embed_call_per_text():
    emb, prov = Emb(delay=0.15), Prov(delay=0.15)
    fn = make_relevance_fn(emb, prov)
    results, _ = run_threads(lambda i: fn(obs()))
    assert len({r.value for r in results}) == 1 and all(r.available for r in results)
    assert len({r.source for r in results}) == 1
    assert prov.count("https://x/p") == 1
    assert emb.count("agent memory") - emb.count("guide") == 1 or emb.count("agent memory") >= 1
    texts = [t for c in emb.calls for t in c if t not in (R._PROBE_TEXT, R._PROBE_TEXT_2)]
    assert len(texts) == len(set(texts)) == 2                       # query + page representation, once each
    assert len([c for c in emb.calls if c[0] == R._PROBE_TEXT]) == 1   # and ONE probe


def test_distinct_queries_on_one_page_share_the_page_call_and_embed_each_text_once():
    emb, prov = Emb(delay=0.05), Prov(delay=0.1)
    fn = make_relevance_fn(emb, prov)
    queries = [f"agent memory {i % 4}" for i in range(N)]
    results, _ = run_threads(lambda i: fn(obs(queries[i])))
    assert all(r.available for r in results) and prov.count("https://x/p") == 1
    texts = [t for c in emb.calls for t in c if t not in (R._PROBE_TEXT, R._PROBE_TEXT_2)]
    assert len(texts) == len(set(texts)) == 4 + 1                   # four distinct queries + one page text


def test_results_equal_the_single_threaded_results():
    emb, prov = Emb(delay=0.02), Prov(delay=0.02)
    fn = make_relevance_fn(emb, prov)
    solo = make_relevance_fn(Emb(), Prov())
    results, _ = run_threads(lambda i: fn(obs(f"agent memory {i % 3}")))
    for i, r in enumerate(results):
        assert r == solo(obs(f"agent memory {i % 3}"))


# ── failure propagation, retry after the TTL ─────────────────────────────────────────────────────────────
def test_leader_embedder_failure_reaches_every_waiter_and_is_retried_only_after_the_ttl():
    clock = [1000.0]
    emb, prov = Emb(delay=0.15), Prov()
    emb.fail_with = RuntimeError("model gone")
    fn = make_relevance_fn(emb, prov, failure_ttl_seconds=30.0, _clock=lambda: clock[0])
    results, _ = run_threads(lambda i: fn(obs()))
    assert {r.source for r in results} == {"unavailable:embedder_error:RuntimeError"} and all(r.value is None for r in results)
    assert emb.count("agent memory") == 1                           # one failed call served all 16 waiters
    before = emb.count("agent memory")
    assert fn(obs()).source == "unavailable:embedder_error:RuntimeError"
    assert emb.count("agent memory") == before                      # cache hit of a failure: no new call
    clock[0] += 29
    assert fn(obs()).source.startswith("unavailable:") and emb.count("agent memory") == before
    emb.fail_with = None
    clock[0] += 2                                                    # 31 s: TTL expired -> retried and repaired
    r = fn(obs())
    assert r.available and emb.count("agent memory") == before + 1


def test_leader_provider_failure_reaches_every_waiter_and_is_retried_after_the_ttl():
    clock = [0.0]
    emb, prov = Emb(), Prov(delay=0.15)
    prov.fail_with = OSError("disk")
    fn = make_relevance_fn(emb, prov, failure_ttl_seconds=30.0, _clock=lambda: clock[0])
    results, _ = run_threads(lambda i: fn(obs()))
    assert {r.source for r in results} == {"unavailable:page_text_provider_error:OSError"}
    assert prov.count("https://x/p") == 1
    clock[0] += 31
    prov.fail_with = None
    assert fn(obs()).available and prov.count("https://x/p") == 2


def test_a_leader_that_raises_a_base_exception_does_not_deadlock_the_waiters():
    class Evil(BaseException):
        pass
    prov = Prov(delay=0.1)
    prov.fail_with = Evil("boom")
    fn = make_relevance_fn(Emb(), prov)
    results, elapsed = run_threads(lambda i: fn(obs()))
    assert all(r.value is None and r.source.startswith("unavailable:") for r in results) and elapsed < 5


def test_provider_returning_none_is_cached_as_no_page_text_without_repeated_calls():
    prov = Prov(pages={})
    fn = make_relevance_fn(Emb(), prov)
    results, _ = run_threads(lambda i: fn(obs()))
    assert {r.source for r in results} == {"unavailable:no_page_text"} and prov.count("https://x/p") == 1


# ── timeouts and cancellation-like behaviour ─────────────────────────────────────────────────────────────
def test_a_hung_embedder_yields_a_bounded_timeout_for_leader_and_waiters_and_is_retried_later():
    clock = [0.0]
    emb, prov = Emb(), Prov()
    emb.hang_on = "agent memory"                                     # the query text hangs
    fn = make_relevance_fn(emb, prov, call_timeout_seconds=0.4, failure_ttl_seconds=30.0, _clock=lambda: clock[0])
    results, elapsed = run_threads(lambda i: fn(obs()), n=8)
    assert {r.source for r in results} == {"unavailable:embedder_timeout"} and elapsed < 4.0
    assert emb.count("agent memory") == 1                            # one hung call, not eight
    emb.release.set()                                                # the late worker result is discarded
    time.sleep(0.1)
    emb.hang_on = None
    assert fn(obs()).source == "unavailable:embedder_timeout"        # cached failure inside the TTL
    clock[0] += 31
    assert fn(obs()).available                                       # recovered after the TTL


def test_a_hung_embed_for_one_text_does_not_block_another_text_or_page():
    emb, prov = Emb(), Prov()
    emb.hang_on = "HANGING"
    fn = make_relevance_fn(emb, prov, call_timeout_seconds=3.0)
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("hung", fn(obs("HANGING query"))))
    t.start()
    assert emb.started.wait(5)
    t0 = time.perf_counter()
    other = fn(obs("agent memory"))                                  # a different key proceeds immediately
    assert other.available and time.perf_counter() - t0 < 1.0
    emb.release.set()
    t.join(10)
    assert not t.is_alive() and box["hung"].available                # the leader finished once released


def test_a_hung_provider_for_one_page_does_not_block_other_pages():
    prov = Prov(pages={"https://x/a": PG, "https://x/b": PG})
    prov.hang_on = "/a"
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=3.0)
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("a", fn(obs(url="https://x/a"))))
    t.start()
    time.sleep(0.2)
    t0 = time.perf_counter()
    assert fn(obs(url="https://x/b")).available and time.perf_counter() - t0 < 1.0
    prov.release.set()
    t.join(10)
    assert box["a"].available


def test_the_provider_timeout_has_its_own_reason():
    prov = Prov()
    prov.hang_on = "/p"
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.3)
    r = fn(obs())
    prov.release.set()
    assert r.source == "unavailable:page_text_provider_timeout"


# ── probe: bounded TTL with recovery, caps, memory ───────────────────────────────────────────────────────
class FlakyProbe(Emb):
    def __init__(self):
        super().__init__()
        self.probe_fails = True

    def embed(self, texts):
        if texts[0] == R._PROBE_TEXT:
            with self.lock:
                self.calls.append(list(texts))
            if self.probe_fails:
                raise TimeoutError("probe")
            return [[1.0, 0.0], [0.0, 1.0]]
        return super().embed(texts)


def test_probe_failure_ttl_default_30s_recovery_repairs_provenance_and_repeat_failures_stay_bounded():
    clock = [0.0]
    emb = FlakyProbe()
    fn = make_relevance_fn(emb, Prov(), _clock=lambda: clock[0])
    probes = lambda: len([c for c in emb.calls if c[0] == R._PROBE_TEXT])     # noqa: E731
    for i in range(20):
        r = fn(obs(f"agent memory {i}"))
        assert r.available and r.provenance_incomplete and r.embedder_revision == "unrecorded"
    assert probes() == 1
    clock[0] += 29.9
    fn(obs("agent memory x"))
    assert probes() == 1
    clock[0] += 0.2                                                  # TTL (30 s) passed: one more probe, fails again
    fn(obs("agent memory y"))
    assert probes() == 2
    for i in range(50):
        clock[0] += 31
        fn(obs(f"agent memory z{i}"))
    assert probes() == 52 and fn.cache_info()["cached_failures"] <= 2          # repeated failure: no growth
    emb.probe_fails = False
    clock[0] += 31
    r = fn(obs("agent memory recovered"))
    assert not r.provenance_incomplete and r.embedder_revision.startswith("probe:")      # success repairs provenance
    assert probes() == 53 and not fn(obs("agent memory again")).provenance_incomplete
    assert probes() == 53


def test_ttl_is_capped_at_300_seconds_whatever_is_requested():
    for requested in (10 ** 6, 1e12):
        clock = [0.0]
        emb = FlakyProbe()
        fn = make_relevance_fn(emb, Prov(), probe_retry_seconds=requested, failure_ttl_seconds=requested,
                               _clock=lambda: clock[0])
        fn(obs())
        clock[0] += 299
        fn(obs("another agent memory"))
        n = len([c for c in emb.calls if c[0] == R._PROBE_TEXT])
        clock[0] += 2                                                # 301 s > cap
        fn(obs("yet another agent memory"))
        assert len([c for c in emb.calls if c[0] == R._PROBE_TEXT]) == n + 1, requested
    assert R.MAX_RETRY_SECONDS == 300.0
    # negative / nonsense values fall back to safe bounds instead of disabling the cache or crashing
    make_relevance_fn(Emb(), Prov(), probe_retry_seconds=-5, failure_ttl_seconds="x", call_timeout_seconds=None)
    make_relevance_fn(Emb(), Prov(), probe_retry_seconds=float("inf"), failure_ttl_seconds=float("nan"))   # non-finite: default


def test_failure_cache_memory_is_bounded_by_cache_size():
    prov = Prov()
    prov.fail_with = OSError("x")
    fn = make_relevance_fn(Emb(), prov, cache_size=8)
    for i in range(500):                                             # 500 DISTINCT failing pages
        assert fn(obs(url=f"https://x/p{i}")).source == "unavailable:page_text_provider_error:OSError"
    info = fn.cache_info()
    assert info["cached_failures"] <= 8 and info["cached"] <= 8 and info["in_flight"] == 0
    for _ in range(500):                                             # one key failing repeatedly: one entry
        fn(obs(url="https://x/same"))
    assert fn.cache_info()["cached_failures"] <= 8 and prov.count("https://x/same") == 1


def test_positive_cache_is_bounded_too_and_no_flight_is_left_behind():
    fn = make_relevance_fn(Emb(), Prov(), cache_size=8)
    for i in range(200):
        fn(obs(f"agent memory topic {i}", f"https://x/p{i}"))
    info = fn.cache_info()
    assert info["cached"] <= 8 and info["in_flight"] == 0


# ── the single-flight primitive itself ───────────────────────────────────────────────────────────────────
def test_single_flight_primitive_shares_one_call_and_drops_stale_flights():
    sf = R._SingleFlight()
    calls = []
    gate = threading.Event()

    def slow():
        calls.append(1)
        gate.wait(5)
        return ("ok", 42)
    out = []
    ts = [threading.Thread(target=lambda: out.append(sf.run("k", slow, 5))) for _ in range(8)]
    [t.start() for t in ts]
    time.sleep(0.2)
    gate.set()
    [t.join(5) for t in ts]
    assert out == [("ok", 42)] * 8 and calls == [1]
    # a stale (hung) flight is abandoned after the timeout and the next request starts a fresh one
    block = threading.Event()
    assert sf.run("h", lambda: (block.wait(5), ("ok", 1))[1], 0.2) == ("error", "timeout")
    assert sf.run("h", lambda: ("ok", 2), 1.0) == ("ok", 2)
    block.set()
    assert sf._flights == {}
