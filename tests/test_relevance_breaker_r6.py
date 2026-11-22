"""SR-04 round 6 (BR-1..BR-3): the dependency circuit breaker must not let one isolated slow page suppress unrelated
healthy pages. The reviewer's scenarios (1,000 pages, fake clock at 10 ms per row, failure TTL 30 s, call timeout
20 ms) with thresholds, plus half-open single-trial, inline waiters and the success-closes-it test.

Measured on the round-5 design for comparison (reviewer): isolated slow page -> 899/999 healthy pages unavailable,
1% slow -> 729/990, 5% slow -> 949/950, flapping -> 700/900."""
from __future__ import annotations

import collections
import random
import re
import threading
import time

import pytest

from agentic_os.content import relevance as R
from agentic_os.content.relevance import PageText, make_relevance_fn
from agentic_os.content.search_signals import SearchObservation

N = 1000
URLS = [f"https://x/{i}" for i in range(N)]


def obs(u, q="agent memory"):
    return SearchObservation(query=q, page_url=u, impressions=100, position=8.0, days_observed=20)


class Emb:
    name = "bow"

    def embed(self, texts):
        return [[float(len(t)), 1.0, float(sum(map(ord, t)) % 7)] for t in texts]


class Prov:
    """Pages in `slow()` hang until released; everything else answers at once."""

    def __init__(self, slow, error=None):
        self.slow, self.error, self.release = slow, error, threading.Event()
        self.lock, self.calls, self.active, self.max_active = threading.Lock(), [], 0, 0

    def page_text(self, u):
        with self.lock:
            self.calls.append(u)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if u in self.slow():
                self.release.wait(30)
            if self.error is not None and u in self.error():
                raise OSError("boom")
            return PageText(title="agent memory guide " + u)
        finally:
            with self.lock:
                self.active -= 1


def run(rows, slow, ttl=30.0, row_dt=0.01, **kw):
    now = [0.0]
    prov = Prov(slow, error=kw.pop("error", None))
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.02, failure_ttl_seconds=ttl,
                           probe_retry_seconds=ttl, _clock=lambda: now[0], **kw)
    reasons, healthy, suppressed = collections.Counter(), 0, 0
    for u in rows:
        now[0] += row_dt
        r = fn(obs(u))
        if u not in slow():
            healthy += 1
            if not r.available:
                suppressed += 1
                reasons[r.source.split(":", 1)[1]] += 1
    info = fn.cache_info()
    prov.release.set()
    return suppressed, healthy, reasons, prov, info


# ── BR-1: isolated and sparse slow pages suppress NOTHING ────────────────────────────────────────────────
@pytest.mark.parametrize("idx", [0, 100, 500, N - 1])
@pytest.mark.parametrize("ttl", [1.0, 5.0, 30.0, 300.0])
def test_br1_one_isolated_slow_page_suppresses_no_healthy_page(idx, ttl):
    suppressed, healthy, reasons, prov, info = run(URLS, lambda: {URLS[idx]}, ttl=ttl)
    assert (suppressed, healthy) == (0, N - 1) and not reasons
    assert info["breaker_open"] == []


def test_br1_one_percent_slow_pages_suppress_nothing():
    rng = random.Random(5)
    slow = set(rng.sample(URLS, 10))
    suppressed, healthy, reasons, prov, info = run(URLS, lambda: slow)
    assert (suppressed, healthy) == (0, N - 10) and info["breaker_open"] == []


def test_br1_five_percent_slow_pages_suppress_nothing_while_under_the_abandoned_thread_cap():
    rng = random.Random(5)
    rng.sample(URLS, 10)
    slow = set(rng.sample(URLS, 50))
    suppressed, healthy, reasons, prov, info = run(URLS, lambda: slow)
    assert (suppressed, healthy) == (0, N - 50) and info["breaker_open"] == [] and info["abandoned_threads"] == 50


def test_br1_ten_percent_slow_pages_are_only_ever_suppressed_by_the_thread_cap_and_with_an_explicit_reason():
    """100 simultaneously hung calls exceed max_abandoned_threads (64): beyond it new calls are refused as 'busy' so
    threads stay bounded. The breaker itself does not open (10% < 50%). With a larger cap nothing is suppressed."""
    rng = random.Random(7)
    slow = set(rng.sample(URLS, 100))
    suppressed, healthy, reasons, prov, info = run(URLS, lambda: slow)
    assert info["breaker_open"] == [] and set(reasons) <= {"page_text_provider_busy"}
    assert info["abandoned_threads"] <= 64
    s2, h2, r2, p2, i2 = run(URLS, lambda: slow, max_abandoned_threads=200)
    assert s2 == 0 and i2["breaker_open"] == []


def test_br1_the_per_key_failure_cache_still_protects_against_re_calling_a_slow_key():
    slow = {URLS[3]}
    now = [0.0]
    prov = Prov(lambda: slow)
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.02, failure_ttl_seconds=30, _clock=lambda: now[0])
    for _ in range(20):
        now[0] += 1
        assert fn(obs(URLS[3])).source in ("unavailable:page_text_provider_timeout", "unavailable:page_text_provider_busy")
    assert prov.calls.count(URLS[3]) == 1
    prov.release.set()


# ── the breaker still opens when the dependency really is down ───────────────────────────────────────────
def test_persistent_outage_opens_after_k_distinct_timeouts_and_costs_a_bounded_number_of_calls():
    suppressed, healthy, reasons, prov, info = run(URLS, lambda: set(URLS))
    assert healthy == 0
    # 5 timeouts open it; afterwards only ONE trial call per (doubling) open period reaches the provider
    assert len(prov.calls) <= 5 + 4 and info["breaker_open"] == ["provider"]
    assert info["abandoned_threads"] <= len(prov.calls)


def test_k_is_configurable_and_the_window_rate_opens_it_for_interleaved_failures():
    # alternating hung / healthy pages: never 5 in a row, but 50% of the last 20 calls time out
    rows = URLS[:100]
    slow = {u for i, u in enumerate(rows) if i % 2 == 0}
    s, h, reasons, prov, info = run(rows, lambda: slow)
    assert "provider" in info["breaker_open"] or s > 0
    assert s > 0 and set(reasons) == {"page_text_provider_circuit_open"}
    s2, h2, r2, p2, i2 = run(rows, lambda: slow, breaker_window=20, breaker_rate=1.0)
    assert s2 == 0 and i2["breaker_open"] == []                       # rate 100% never reached: it stays closed
    s3, h3, r3, p3, i3 = run(URLS[:30], lambda: set(URLS[:30]), breaker_threshold=2)
    assert len(p3.calls) <= 2 + 1


def test_ordinary_errors_are_responses_not_hangs_and_never_open_the_breaker():
    err = set(URLS[:300])
    suppressed, healthy, reasons, prov, info = run(URLS[:300], lambda: set(), error=lambda: err)
    assert info["breaker_open"] == [] and len(prov.calls) == 300


def test_many_callers_hanging_on_one_key_count_as_one_timeout():
    prov = Prov(lambda: {"https://x/h"})
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.2)
    barrier = threading.Barrier(16)
    out = []

    def w():
        barrier.wait(5)
        out.append(fn(obs("https://x/h")))
    ts = [threading.Thread(target=w) for _ in range(16)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert len(out) == 16 and fn.cache_info()["breaker_open"] == [] and len(prov.calls) == 1
    prov.release.set()


# ── flapping: measured and reported honestly ─────────────────────────────────────────────────────────────
def test_flapping_provider_is_measured_and_the_bound_is_stated():
    """Two outages (60 and 40 consecutive hanging rows) in 1,000 rows at 10 ms/row; default open period 1 s then
    doubling. Healthy pages are suppressed only while the breaker is open and until its first trial: the bound below
    is the measured value plus headroom (reviewer's round-5 measurement was 700 of 900)."""
    outage = {u for i, u in enumerate(URLS) if 200 <= i < 260 or 500 <= i < 540}
    suppressed, healthy, reasons, prov, info = run(URLS, lambda: outage)
    print(f"flapping: {suppressed}/{healthy} healthy pages unavailable ({100 * suppressed / healthy:.1f}%) {dict(reasons)}")
    assert set(reasons) <= {"page_text_provider_circuit_open", "page_text_provider_busy"}
    assert suppressed <= 0.15 * healthy, (suppressed, healthy)       # measured 110 / 900 (12%) with the 1 s base
    assert info["breaker_open"] == []                                  # recovered by the end of the run


# ── BR-2: a true single-trial half-open ──────────────────────────────────────────────────────────────────
def _open_breaker(fn, now, prov_slow, n=5):
    for i in range(n):
        now[0] += 0.01
        assert fn(obs(f"https://x/down{i}")).source == "unavailable:page_text_provider_timeout"


def test_br2_half_open_lets_exactly_one_trial_call_through_and_other_keys_fail_fast():
    now = [0.0]
    down = set(f"https://x/down{i}" for i in range(5))
    prov = Prov(lambda: down)
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.02, _clock=lambda: now[0], breaker_base_seconds=2.0)
    _open_breaker(fn, now, down)
    assert fn.cache_info()["breaker"]["provider"] == "open"
    now[0] += 1.0
    assert fn(obs("https://x/fresh")).source == "unavailable:page_text_provider_circuit_open"
    calls_before = len(prov.calls)
    now[0] += 1.5                                                       # open period over: half-open
    assert fn.cache_info()["breaker"]["provider"] == "half-open"
    gate = threading.Event()
    slow_trial = {"https://x/trial"}
    prov.slow = lambda: slow_trial | down
    results = {}
    t = threading.Thread(target=lambda: results.setdefault("trial", fn(obs("https://x/trial"))))
    t.start()
    deadline = time.time() + 5
    while len(prov.calls) == calls_before and time.time() < deadline:
        time.sleep(0.002)
    others = [fn(obs(f"https://x/other{i}")) for i in range(8)]          # while the trial is in flight
    assert {r.source for r in others} == {"unavailable:page_text_provider_circuit_open"}
    t.join(10)
    assert len(prov.calls) == calls_before + 1                           # exactly ONE call reached the provider
    prov.release.set()
    gate.set()


def test_br2_the_first_response_closes_the_breaker_and_a_failed_trial_reopens_it_with_backoff():
    now = [0.0]
    down = set(f"https://x/down{i}" for i in range(5)) | {"https://x/t1"}
    prov = Prov(lambda: down)
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.02, _clock=lambda: now[0], breaker_base_seconds=2.0)
    _open_breaker(fn, now, down)
    now[0] += 2.1
    assert fn(obs("https://x/t1")).source == "unavailable:page_text_provider_timeout"      # the trial FAILS
    assert fn.cache_info()["breaker"]["provider"] == "open"
    now[0] += 3.0                                                        # < doubled period (4 s)
    assert fn(obs("https://x/fresh1")).source == "unavailable:page_text_provider_circuit_open"
    now[0] += 1.5                                                        # past 4 s: half-open again
    assert fn(obs("https://x/ok1")).available                            # trial succeeds ...
    assert fn.cache_info()["breaker"]["provider"] == "closed"            # ... and closes it
    assert fn(obs("https://x/ok2")).available and fn(obs("https://x/ok3")).available
    prov.release.set()


def test_r8_a_success_closes_the_breaker_and_resets_the_failure_counters_ttl_expiry_does_not_mask_it():
    now = [0.0]
    down = set(f"https://x/down{i}" for i in range(5)) | set(f"https://x/again{i}" for i in range(4))
    prov = Prov(lambda: down)
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.02, _clock=lambda: now[0], breaker_base_seconds=2.0)
    _open_breaker(fn, now, down)
    now[0] += 2.1
    assert fn(obs("https://x/healthy")).available                        # the trial succeeds
    state = fn.cache_info()["breaker"]["provider"]
    assert state == "closed", state                                      # closed by the SUCCESS, not by the clock
    now[0] += 1000.0                                                     # and long expiry changes nothing
    assert fn.cache_info()["breaker"]["provider"] == "closed"
    for i in range(4):                                                   # k-1 fresh timeouts: counters were reset
        assert fn(obs(f"https://x/again{i}")).source == "unavailable:page_text_provider_timeout"
    assert fn.cache_info()["breaker_open"] == []
    prov.release.set()


# ── BR-3: inline waiter timeouts never trip it ───────────────────────────────────────────────────────────
def test_br3_inline_waiter_timeouts_while_the_leader_is_healthy_do_not_open_the_breaker():
    class SlowOK:
        def __init__(self):
            self.n = 0
            self.lock = threading.Lock()

        def page_text(self, u):
            with self.lock:
                self.n += 1
            time.sleep(1.5)
            return PageText(title="agent memory guide")
    prov = SlowOK()
    fn = make_relevance_fn(Emb(), prov, thread_affine=True, call_timeout_seconds=0.1, breaker_threshold=1)
    leader = threading.Thread(target=lambda: fn(obs("https://x/p")))
    leader.start()
    time.sleep(0.1)
    waiters = [fn(obs("https://x/p")) for _ in range(6)]                 # each stops waiting after 0.1 s
    assert {w.source for w in waiters} == {"unavailable:page_text_provider_timeout"}
    assert fn.cache_info()["breaker_open"] == []                         # even with threshold 1
    leader.join(10)
    assert prov.n == 1 and fn(obs("https://x/p")).available


# ── an ordinary error is a RESPONSE: it resets the consecutive-timeout count and closes a half-open breaker ─────
def test_an_ordinary_error_between_timeouts_resets_the_consecutive_count():
    now = [0.0]
    hung = {f"https://x/h{i}" for i in range(8)}
    boom = {"https://x/err"}
    prov = Prov(lambda: hung, error=lambda: boom)
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.02, _clock=lambda: now[0])
    for i in range(4):
        assert fn(obs(f"https://x/h{i}")).source == "unavailable:page_text_provider_timeout"
    assert fn(obs("https://x/err")).source == "unavailable:page_text_provider_error:OSError"     # a response
    for i in range(4, 8):                                           # 4 more: 8 timeouts in all, but never 5 in a row
        assert fn(obs(f"https://x/h{i}")).source == "unavailable:page_text_provider_timeout"
    assert fn.cache_info()["breaker_open"] == []
    prov.release.set()


def test_an_ordinary_error_as_the_half_open_trial_closes_the_breaker():
    now = [0.0]
    down = {f"https://x/down{i}" for i in range(5)}
    boom = {"https://x/trial-err"}
    prov = Prov(lambda: down, error=lambda: boom)
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.02, _clock=lambda: now[0], breaker_base_seconds=2.0)
    _open_breaker(fn, now, down)
    now[0] += 2.5
    assert fn(obs("https://x/trial-err")).source == "unavailable:page_text_provider_error:OSError"
    assert fn.cache_info()["breaker"]["provider"] == "closed"        # it answered, however unhappily
    assert fn(obs("https://x/fine")).available
    prov.release.set()
