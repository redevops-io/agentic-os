"""SR-04 round 6: no double execution in single-flight (DE-1), thread-affine mode lets KeyboardInterrupt / SystemExit
propagate (TA-1), an abandoned worker keeps ITS key busy instead of running concurrently with it."""
from __future__ import annotations

import threading
import time

import pytest

from agentic_os.content import relevance as R
from agentic_os.content.relevance import PageText, make_relevance_fn
from agentic_os.content.search_signals import SearchObservation


def obs(q="agent memory", u="https://x/p"):
    return SearchObservation(query=q, page_url=u, impressions=100, position=8.0, days_observed=20)


class Meter:
    """Counts executions and the maximum number of concurrent executions of a call."""

    def __init__(self):
        self.lock, self.calls, self.active, self.max_active = threading.Lock(), 0, 0, 0

    def enter(self):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def leave(self):
        with self.lock:
            self.active -= 1


class SlowProv:
    def __init__(self, delay, meter=None):
        self.delay, self.m = delay, meter or Meter()

    def page_text(self, u):
        self.m.enter()
        try:
            time.sleep(self.delay)
            return PageText(title="agent memory guide")
        finally:
            self.m.leave()


class Emb:
    name = "bow"

    def embed(self, texts):
        return [[float(len(t)), 1.0, float(sum(map(ord, t)) % 7)] for t in texts]


# ── DE-1 ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_de1_poc_class_a_slow_healthy_inline_leader_is_never_executed_twice():
    sf = R._SingleFlight()
    m = Meter()
    out = {}

    def slow():
        m.enter()
        try:
            time.sleep(2.2)
            return ("ok", "slow")
        finally:
            m.leave()
    t = threading.Thread(target=lambda: out.setdefault("leader", sf.run("k", slow, 0.3, inline=True)))
    t.start()
    time.sleep(1.5)                                        # older than timeout + 1 s: the old age rule fired here
    t0 = time.time()
    second = sf.run("k", slow, 0.3, inline=True)
    waited = time.time() - t0
    t.join(5)
    assert m.calls == 1 and m.max_active == 1, (m.calls, m.max_active)
    assert second == ("error", "timeout") and waited < 0.8             # a waiter just stops waiting
    assert out["leader"] == ("ok", "slow")                             # the healthy leader still succeeded


def test_de1_through_make_relevance_fn_thread_affine_slow_provider_runs_exactly_once():
    meter = Meter()
    fn = make_relevance_fn(Emb(), SlowProv(2.0, meter), thread_affine=True, call_timeout_seconds=0.3)
    results = {}
    leader = threading.Thread(target=lambda: results.setdefault("leader", fn(obs())))
    leader.start()
    time.sleep(1.4)                                                    # > timeout + 1 s
    late = fn(obs())                                                   # same page, another caller
    leader.join(10)
    assert meter.calls == 1 and meter.max_active == 1, (meter.calls, meter.max_active)
    assert results["leader"].available                                  # the healthy leader succeeded
    assert late.source == "unavailable:page_text_provider_timeout"      # bounded wait, explicit, no second execution
    assert fn(obs()).available and meter.calls == 1                     # afterwards the cached result serves everyone


def test_de1_many_late_callers_still_see_one_execution_and_the_leader_is_not_interrupted():
    meter = Meter()
    fn = make_relevance_fn(Emb(), SlowProv(2.2, meter), thread_affine=True, call_timeout_seconds=0.2)
    box = {}
    leader = threading.Thread(target=lambda: box.setdefault("l", fn(obs())))
    leader.start()
    time.sleep(0.1)
    late = []
    threads = [threading.Thread(target=lambda: late.append(fn(obs()))) for _ in range(8)]
    [t.start() for t in threads]
    time.sleep(1.6)
    threads += [threading.Thread(target=lambda: late.append(fn(obs()))) for _ in range(4)]
    [t.start() for t in threads[8:]]
    [t.join(10) for t in threads]
    leader.join(10)
    assert meter.calls == 1 and meter.max_active == 1
    assert box["l"].available
    assert {r.source for r in late if not r.available} <= {"unavailable:page_text_provider_timeout"}


def test_de1_threaded_mode_an_abandoned_worker_keeps_its_key_busy_and_is_never_run_concurrently():
    meter = Meter()
    prov = SlowProv(1.2, meter)
    fn = make_relevance_fn(Emb(), prov, call_timeout_seconds=0.2, failure_ttl_seconds=0)
    assert fn(obs()).source == "unavailable:page_text_provider_timeout"
    for _ in range(5):                                                 # TTL 0: the cache never masks the key
        assert fn(obs()).source == "unavailable:page_text_provider_busy"
    assert meter.calls == 1 and meter.max_active == 1
    assert fn(obs(u="https://x/other")).source in ("unavailable:page_text_provider_timeout",)     # other keys unaffected
    deadline = time.time() + 6
    while fn.cache_info()["abandoned_threads"] and time.time() < deadline:
        time.sleep(0.05)
    prov.delay = 0.0
    assert fn(obs()).available                                          # once the worker is gone the key works again


def test_de1_the_age_rule_still_applies_to_threaded_flights_and_dead_inline_leaders():
    sf = R._SingleFlight()
    old = R._Flight(inline=False)
    old.started -= 100.0
    sf._flights["k"] = old
    assert sf.run("k", lambda: ("ok", 1), 0.5) == ("ok", 1)
    live_inline = R._Flight(inline=True)                                # leader = this live thread, ancient flight
    live_inline.started -= 1000.0
    sf._flights["j"] = live_inline
    t0 = time.time()
    assert sf.run("j", lambda: ("ok", 9), 0.3, inline=True) == ("error", "timeout")      # NOT taken over
    assert time.time() - t0 < 1.0 and sf._flights["j"] is live_inline
    sf._flights.pop("j")
    dead = {}
    t = threading.Thread(target=lambda: dead.setdefault("f", sf._flights.setdefault("d", R._Flight(inline=True))))
    t.start()
    t.join()
    assert sf.run("d", lambda: ("ok", 5), 5.0, inline=True) == ("ok", 5)                  # dead leader: replaced


# ── TA-1 ─────────────────────────────────────────────────────────────────────────────────────────────────
class RaisingProv:
    def __init__(self, exc):
        self.exc = exc

    def page_text(self, u):
        raise self.exc


class RaisingEmb(Emb):
    def __init__(self, exc):
        self.exc = exc

    def embed(self, texts):
        if texts[0] not in (R._PROBE_TEXT,):
            raise self.exc
        return super().embed(texts)


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(3)])
def test_ta1_thread_affine_provider_base_exceptions_propagate_after_cleanup(exc):
    fn = make_relevance_fn(Emb(), RaisingProv(exc), thread_affine=True)
    with pytest.raises(type(exc)):
        fn(obs())
    info = fn.cache_info()
    assert info["in_flight"] == 0 and info["cached_failures"] == 0           # flight removed, nothing cached
    with pytest.raises(type(exc)):
        fn(obs())                                                           # still raised, not memoised as a failure


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(3)])
def test_ta1_thread_affine_embedder_base_exceptions_propagate_after_cleanup(exc):
    fn = make_relevance_fn(RaisingEmb(exc), SlowProv(0.0), thread_affine=True)
    with pytest.raises(type(exc)):
        fn(obs())
    assert fn.cache_info()["in_flight"] == 0


def test_ta1_ordinary_exceptions_stay_explicit_unavailable_results_in_thread_affine_mode():
    for exc, reason in ((OSError("x"), "unavailable:page_text_provider_error:OSError"),
                        (ValueError("x"), "unavailable:page_text_provider_error:ValueError")):
        r = make_relevance_fn(Emb(), RaisingProv(exc), thread_affine=True)(obs())
        assert r.value is None and r.source == reason
    r = make_relevance_fn(RaisingEmb(RuntimeError("x")), SlowProv(0.0), thread_affine=True)(obs())
    assert r.source == "unavailable:embedder_error:RuntimeError"


def test_ta1_waiters_of_an_interrupted_inline_leader_get_an_explicit_outcome_and_the_key_recovers():
    sf = R._SingleFlight()
    go, res = threading.Event(), []

    def leader_fn():
        go.wait(5)
        raise KeyboardInterrupt()
    lt_exc = []

    def leader():
        try:
            sf.run("k", leader_fn, 5.0, inline=True)
        except BaseException as e:  # noqa: BLE001
            lt_exc.append(type(e).__name__)
    lt = threading.Thread(target=leader)
    lt.start()
    while "k" not in sf._flights:
        time.sleep(0.002)
    waiters = [threading.Thread(target=lambda: res.append(sf.run("k", lambda: ("ok", 9), 5.0, inline=True)))
               for _ in range(4)]
    [w.start() for w in waiters]
    time.sleep(0.2)
    go.set()
    [w.join(5) for w in waiters]
    lt.join(5)
    assert lt_exc == ["KeyboardInterrupt"] and res == [("error", "interrupted")] * 4 and sf._flights == {}
    assert sf.run("k", lambda: ("ok", 2), 1.0, inline=True) == ("ok", 2)


def test_ta1_threaded_mode_still_turns_base_exceptions_in_the_worker_into_explicit_unavailable_results():
    r = make_relevance_fn(Emb(), RaisingProv(SystemExit(3)))(obs())
    assert r.value is None and r.source == "unavailable:page_text_provider_error:SystemExit" or \
        r.source == "unavailable:exception:SystemExit"
