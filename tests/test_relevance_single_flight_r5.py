"""SR-04 round 5 (reviewer's findings SF-1..SF-4): interrupted leaders, circuit breaker + abandoned-thread cap,
duplicate-call stress, thread-affine mode."""
from __future__ import annotations

import ctypes
import hashlib
import random
import re
import sqlite3
import sys
import threading
import time

import pytest

from agentic_os.content import relevance as R
from agentic_os.content.relevance import PageText, make_relevance_fn
from agentic_os.content.search_signals import SearchObservation

PG = PageText(title="Agent memory guide", body_text="how agents remember things")


def obs(q="agent memory", u="https://x/p"):
    return SearchObservation(query=q, page_url=u, impressions=100, position=8.0, days_observed=20)


class Emb:
    name = "bow"

    def __init__(self, hang=None):
        self.lock, self.calls, self.hang = threading.Lock(), [], hang
        self.release = threading.Event()

    def embed(self, texts):
        with self.lock:
            self.calls.append(tuple(texts))
        if self.hang and any(self.hang in t for t in texts):
            self.release.wait(30)
        out = []
        for t in texts:
            v = [0.0] * 64
            for w in re.findall(r"\w+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
            out.append(v)
        return out


class Prov:
    def __init__(self, hang=False, delay=0.0):
        self.lock, self.calls, self.hang, self.delay = threading.Lock(), [], hang, delay
        self.release = threading.Event()
        self.threads = set()

    def page_text(self, u):
        with self.lock:
            self.calls.append(u)
            self.threads.add(threading.get_ident())
        if self.hang:
            self.release.wait(30)
        if self.delay:
            time.sleep(self.delay)
        return PG


def async_raise(thread, exc=KeyboardInterrupt):
    return ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread.ident), ctypes.py_object(exc))


# ── SF-1: an interrupted leader never leaves its flight behind ───────────────────────────────────────────
def test_sf1_poc_a_leader_interrupted_while_waiting_does_not_poison_the_key():
    sf = R._SingleFlight()
    started, out = threading.Event(), []

    def slow():
        started.set()
        time.sleep(1.0)
        return ("ok", 1)

    def leader():
        try:
            out.append(sf.run("k", slow, 5.0))
        except BaseException as e:  # noqa: BLE001
            out.append(type(e).__name__)
    t = threading.Thread(target=leader)
    t.start()
    started.wait(5)
    time.sleep(0.05)
    async_raise(t)
    t.join(3)
    assert out == ["KeyboardInterrupt"] and sf._flights == {}           # the flight was removed by the finally
    t0 = time.time()
    assert sf.run("k", lambda: ("ok", 2), 0.5) == ("ok", 2) and time.time() - t0 < 0.4


def test_sf1_waiters_get_an_explicit_interrupted_outcome_when_the_leaders_wait_is_interrupted(monkeypatch):
    """The leader's wait (`worker.join`) is interrupted by a BaseException (as a KeyboardInterrupt in the main thread
    would do): the flight is removed, the abandoned worker is counted, the waiters blocked on it are woken at once
    with ('error', 'interrupted') (not after the timeout), and the exception reaches the leader."""
    sf = R._SingleFlight()
    release, go = threading.Event(), threading.Event()
    real_join = threading.Thread.join

    def join(self, timeout=None):
        if self.daemon and not go.is_set() and getattr(self, "_leader_join", True):
            go.wait(10)                                           # the leader sits in join(); then it is interrupted
            raise KeyboardInterrupt()
        return real_join(self, timeout)
    monkeypatch.setattr(threading.Thread, "join", join)
    res, leader_exc = [], []

    def leader():
        try:
            sf.run("k", lambda: (release.wait(10), ("ok", 1))[1], 30.0)
        except BaseException as e:  # noqa: BLE001
            leader_exc.append(type(e).__name__)
    lt = threading.Thread(target=leader)
    lt.start()
    while "k" not in sf._flights:
        time.sleep(0.002)
    waiters = [threading.Thread(target=lambda: res.append(sf.run("k", lambda: ("ok", 9), 30.0))) for _ in range(6)]
    [w.start() for w in waiters]
    time.sleep(0.2)
    t0 = time.time()
    go.set()
    [w.join(5) for w in waiters]
    real_join(lt, 5)
    assert res == [("error", "interrupted")] * 6 and time.time() - t0 < 2.0       # woken at once, not after 30 s
    assert leader_exc == ["KeyboardInterrupt"] and sf._flights == {} and sf.abandoned_alive() == 1
    assert sf.run("k", lambda: ("ok", 2), 1.0) == ("error", "busy")               # not run concurrently with its worker
    release.set()
    deadline = time.time() + 5
    while sf.abandoned_alive() and time.time() < deadline:
        time.sleep(0.01)
    assert sf.run("k", lambda: ("ok", 2), 1.0) == ("ok", 2)                       # the key is not poisoned


def test_sf1_stress_repeated_async_exceptions_at_random_points_never_poison_a_key():
    sf = R._SingleFlight()
    stop = threading.Event()
    done = []

    def worker_loop():
        for _ in range(50):                                   # a late async exception may land outside the inner try
            try:
                while not stop.is_set():
                    try:
                        sf.run("k", lambda: (time.sleep(0.0005), ("ok", 1))[1], 0.5,
                               cached=lambda: None, on_done=lambda o: None)
                    except BaseException:  # noqa: BLE001
                        pass
                break
            except BaseException:  # noqa: BLE001
                continue
        done.append(1)
    t = threading.Thread(target=worker_loop)
    t.start()
    rng = random.Random(5)
    for _ in range(600):
        time.sleep(rng.uniform(0, 0.002))
        async_raise(t)
    stop.set()
    t.join(5)
    assert not t.is_alive()
    t0 = time.time()
    assert sf.run("k", lambda: ("ok", 2), 0.5) == ("ok", 2)               # a leftover flight (dead leader) is replaced
    assert time.time() - t0 < 1.0
    assert sf.run("other", lambda: ("ok", 3), 0.5) == ("ok", 3) and set(sf._flights) <= {"k"}


# ── SF-2: circuit breaker and abandoned-thread cap ───────────────────────────────────────────────────────
def test_sf2_200_distinct_hung_rows_cost_one_timeout_and_a_bounded_number_of_threads():
    before = threading.active_count()
    prov = Prov(hang=True)
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.2, failure_ttl_seconds=30, breaker_threshold=1,
                           breaker_base_seconds=30)
    t0 = time.time()
    results = [fn(obs(u=f"https://x/p{i}")) for i in range(200)]
    elapsed = time.time() - t0
    assert elapsed < 2.0 and threading.active_count() - before <= 3
    assert results[0].source == "unavailable:page_text_provider_timeout"
    assert {r.source for r in results[1:]} == {"unavailable:page_text_provider_circuit_open"}
    assert all(r.value is None for r in results)                           # explicit unavailable, never a zero
    assert len(prov.calls) == 1
    info = fn.cache_info()
    assert info["abandoned_threads"] == 1 and info["breaker_open"] == ["provider"]
    prov.release.set()


def test_sf2_breaker_recovers_after_the_ttl_and_a_success_closes_it():
    clock = [0.0]
    prov = Prov(hang=True)
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.2, failure_ttl_seconds=30, breaker_threshold=1,
                           breaker_base_seconds=30, _clock=lambda: clock[0])
    assert fn(obs()).source == "unavailable:page_text_provider_timeout"
    assert fn(obs(u="https://x/other")).source == "unavailable:page_text_provider_circuit_open"
    prov.release.set()
    prov.hang = False
    time.sleep(0.1)
    clock[0] += 29
    assert fn(obs(u="https://x/other")).source == "unavailable:page_text_provider_circuit_open"
    clock[0] += 2                                                        # TTL expired: one trial call is allowed
    r = fn(obs(u="https://x/other"))
    assert r.available and fn.cache_info()["breaker_open"] == []
    assert fn(obs(u="https://x/third")).available                         # the breaker stays closed


def test_sf2_embedder_breaker_covers_every_text_and_the_probe():
    emb = Emb(hang="agent memory")
    fn = make_relevance_fn(emb, Prov(), call_timeout_seconds=0.2, failure_ttl_seconds=30, breaker_threshold=1,
                           breaker_base_seconds=30)
    r1 = fn(obs("agent memory one"))
    assert r1.source == "unavailable:embedder_timeout"
    rest = [fn(obs(f"agent memory {i}")) for i in range(50)]
    assert {r.source for r in rest} == {"unavailable:embedder_circuit_open"}
    assert fn.cache_info()["breaker_open"] == ["embedder"]
    emb.release.set()


def test_sf2_the_number_of_abandoned_threads_is_capped_with_an_explicit_busy_reason():
    before = threading.active_count()
    prov = Prov(hang=True)
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.1, failure_ttl_seconds=0, max_abandoned_threads=3)
    results = [fn(obs(u=f"https://x/p{i}")) for i in range(40)]
    assert threading.active_count() - before <= 3
    assert [r.source for r in results[:3]] == ["unavailable:page_text_provider_timeout"] * 3
    assert {r.source for r in results[3:]} == {"unavailable:page_text_provider_busy"}
    assert fn.cache_info()["abandoned_threads"] == 3
    prov.release.set()
    time.sleep(0.3)
    assert fn.cache_info()["abandoned_threads"] == 0                       # finished abandoned workers stop counting
    prov.hang = False
    assert fn(obs(u="https://x/fresh")).available


def test_sf2_late_results_of_abandoned_calls_are_never_cached():
    prov = Prov(delay=0.5)
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.1, failure_ttl_seconds=30)
    assert fn(obs()).source == "unavailable:page_text_provider_timeout"
    time.sleep(0.6)
    assert fn.cache_info()["cached"] == 0
    assert fn(obs()).source in ("unavailable:page_text_provider_timeout", "unavailable:page_text_provider_circuit_open")


# ── SF-3: no duplicate calls, even at a 1-microsecond thread switch interval ─────────────────────────────
def test_sf3_no_duplicate_provider_or_embed_calls_under_aggressive_thread_switching():
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        dup = 0
        for _ in range(150):
            prov, emb = Prov(), Emb()
            fn = make_relevance_fn(emb, prov)
            barrier = threading.Barrier(16)

            def w():
                barrier.wait(5)
                fn(obs())
            ts = [threading.Thread(target=w) for _ in range(16)]
            [t.start() for t in ts]
            [t.join(10) for t in ts]
            singles = [c for c in emb.calls if len(c) == 1]
            if len(prov.calls) != 1 or len(singles) != 2 or len([c for c in emb.calls if len(c) == 2]) != 1:
                dup += 1
        assert dup == 0, dup
    finally:
        sys.setswitchinterval(old)


def test_sf3_the_cache_write_happens_inside_the_flight():
    sf = R._SingleFlight()
    cache, order = {}, []
    calls = []

    def fn():
        calls.append(1)
        return ("ok", 7)

    def on_done(o):
        order.append(("cache", threading.current_thread().name, "k" in sf._flights))      # still registered
        cache["k"] = o
    out = sf.run("k", fn, 2.0, cached=lambda: cache.get("k"), on_done=on_done)
    assert out == ("ok", 7) and order == [("cache", order[0][1], True)]
    assert sf.run("k", fn, 2.0, cached=lambda: cache.get("k"), on_done=on_done) == ("ok", 7) and calls == [1]


# ── SF-4: thread-affine mode ─────────────────────────────────────────────────────────────────────────────
def _sqlite_provider():
    conn = sqlite3.connect(":memory:")                                    # created on the CALLER thread
    conn.execute("create table pages(url text, title text)")
    conn.execute("insert into pages values('https://x/p','agent memory guide')")

    class P:
        def page_text(self, u):
            row = conn.execute("select title from pages where url=?", (u,)).fetchone()
            return PageText(title=row[0]) if row else None
    return P()


def test_sf4_default_mode_breaks_a_thread_affine_provider_and_says_so_explicitly():
    r = make_relevance_fn(Emb(), _sqlite_provider())(obs())
    assert r.value is None and r.source == "unavailable:page_text_provider_error:ProgrammingError"


def test_sf4_thread_affine_mode_runs_in_the_callers_thread_and_works():
    prov = Prov()
    before = threading.active_count()
    fn = make_relevance_fn(Emb(), _sqlite_provider(), thread_affine=True)
    assert fn(obs()).available
    assert threading.active_count() == before                              # no worker threads at all
    fn2 = make_relevance_fn(Emb(), prov, thread_affine=True)
    fn2(obs())
    assert prov.threads == {threading.get_ident()}


def test_sf4_thread_affine_mode_does_not_enforce_the_timeout_and_still_shares_results():
    prov = Prov(delay=0.4)
    fn = make_relevance_fn(Emb(), prov, thread_affine=True, call_timeout_seconds=0.1)
    r = fn(obs())
    assert r.available and len(prov.calls) == 1                             # a slow call is NOT cut off (documented)
    assert fn.cache_info()["abandoned_threads"] == 0 and fn.cache_info()["breaker_open"] == []
    barrier = threading.Barrier(8)
    out = []

    def w():
        barrier.wait(5)
        out.append(fn(obs(u="https://x/q")))
    ts = [threading.Thread(target=w) for _ in range(8)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert len(prov.calls) == 2 and all(r.available or r.source.endswith("timeout") for r in out)


# ── SF-1 follow-up: a leftover flight (the cleanup itself was interrupted) never blocks a key ────────────
def test_sf1_a_flight_left_behind_by_a_dead_leader_is_replaced_immediately():
    sf = R._SingleFlight()
    t = threading.Thread(target=lambda: sf._flights.setdefault("k", R._Flight()))
    t.start()
    t.join()                                                         # the leader thread is gone, the flight stays
    assert "k" in sf._flights and not sf._flights["k"].leader.is_alive()
    t0 = time.time()
    assert sf.run("k", lambda: ("ok", 2), 30.0) == ("ok", 2) and time.time() - t0 < 1.0


def test_sf1_a_flight_older_than_the_timeout_plus_margin_is_replaced_even_if_its_leader_is_alive():
    sf = R._SingleFlight()
    stale = R._Flight()                                              # leader = this (alive) thread
    stale.started -= 100.0
    sf._flights["k"] = stale
    t0 = time.time()
    assert sf.run("k", lambda: ("ok", 3), 0.5) == ("ok", 3) and time.time() - t0 < 1.0
    assert stale.outcome == ("error", "interrupted") and stale.done.is_set()
    fresh = R._Flight()                                              # a young flight of a live leader is NOT replaced
    sf._flights["j"] = fresh
    assert sf.run("j", lambda: ("ok", 4), 0.3) == ("error", "timeout")
    sf._flights.pop("j", None)
