"""The reconciliation supervisor: bounded, read-only probing of unknown outcomes; mission ownership with fencing; independent progress.

The invariant under test, in every scenario: only positive execution evidence or an AUTHORITATIVE negative resolves an unknown outcome.
Repeating the read-only probe is allowed; repeating the business action while the outcome is unknown never is, and exhausting the
retry budget never turns uncertainty into permission to resend."""
from __future__ import annotations

import random
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_mission_park_resume import Crash, EVIDENCE, Plan, Provider, World, crash_then_restart, events, handlers, registry   # noqa: E402

from agentic_os.mission.ownership import FencedEventStore, FencingError, MissionLeases                  # noqa: E402
from agentic_os.mission.probes import (                                                                  # noqa: E402
    ProbeOutcome as O, ProbeRegistry, ProbeResult, Qualification, from_http_status)
from agentic_os.mission.reconcile_jobs import PRESETS, ReconcileJobs, ReconciliationSupervisor, RetryPolicy   # noqa: E402
from agentic_os.mission.runtime import MissionRuntime                                                    # noqa: E402
from agentic_os.mission.store import EventStore                                                          # noqa: E402
from agentic_os.mission.types import MissionState                                                        # noqa: E402
from agentic_os.mission.waitstate import Wait, classify                                                  # noqa: E402

POLICY = RetryPolicy(max_probes=5, initial_s=30, max_s=300, factor=2.0, jitter=0.0)


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


EV = {"kind": "provider_record", "reference": "provider ledger entry pi_second"}
Q_STRICT = Qualification("ledgerco", eventually_consistent=False, authoritative_negative=True)
Q_EVENTUAL = Qualification("searchco", eventually_consistent=True, visibility_window_s=600, authoritative_negative=True)
Q_WEAK = Qualification("weakco")                                # the defaults: eventually consistent, no negative authority


def executed(ref="pi_second", result="default", ev=EV):
    return lambda ctx: ProbeResult(O.CONFIRMED_EXECUTED, evidence=dict(ev) if ev else None, provider_ref=ref,
                                   result={"charge": ref} if result == "default" else result, observed_at=ctx.now)


def absent(authoritative=True):
    return lambda ctx: ProbeResult(O.CONFIRMED_NOT_EXECUTED, evidence={"kind": "provider_record", "reference": "search by key: 0 results"},
                                   authoritative_negative=authoritative, observed_at=ctx.now)


def inconclusive(detail="not found"):
    return lambda ctx: ProbeResult(O.INCONCLUSIVE, detail=detail)


def transient(retry_after=None):
    return lambda ctx: ProbeResult(O.TRANSIENT_ERROR, retry_after_s=retry_after, detail="provider answered 503")


def build(tmp_path, monkeypatch, *, probes=(), chain=True, mode="crash_before_effect", policy=POLICY, notifier=None, leases=None, jobs=None,
          deep=False):
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch, mode=mode, chain=chain, deep=deep)
    rt.run(mid)                                                   # parks (and runs anything independent of the unknown node)
    clock = Clock()
    jobs = jobs or ReconcileJobs(str(tmp_path / "jobs.db"))
    reg = ProbeRegistry()
    for pat, probe, q in probes:
        reg.register(pat, probe, q)
    sup = ReconciliationSupervisor(rt, jobs, reg, policy=policy, owner="sup-1", clock=clock, rng=random.Random(1), notifier=notifier,
                                   mission_leases=leases)
    sup.scan()
    return SimpleNamespace(w=w, mid=mid, rt=rt, jobs=jobs, reg=reg, sup=sup, clock=clock, tmp=tmp_path,
                           lease=lambda: rt.repo.pending_reconciliation(mid)[0]["lease_id"] if rt.repo.pending_reconciliation(mid)
                           else jobs.list()[0]["job_id"])


def tick(x, after):
    x.clock.advance(after)
    return x.sup.tick()


def second_calls(x):
    return x.w.provider.calls.count("second")


# ── the policy table, row by row ─────────────────────────────────────────────────────────
def test_confirmed_executed_settles_restores_the_result_and_resumes_the_same_mission(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, mode="crash_after_effect", probes=[("op.*", executed(), Q_STRICT)])
    rep = tick(x, 31)
    assert rep.resolved == [x.lease()] and x.rt._missions[x.mid].state == MissionState.SUCCEEDED
    assert second_calls(x) == 0 and x.w.provider.effects == {"first": 1, "second": 1, "third": 1}       # nothing resent
    assert x.rt._world(x.mid).get("done2") == {"charge": "pi_second", "reconciled": True, "reconciled_by": "system:reconciliation:ledgerco",
                                               "evidence": events(x.rt, x.mid, "ReconciliationResolved")[0]["evidence"]}
    res = events(x.rt, x.mid, "ReconciliationResolved")[0]
    assert res["actor"] == "system:reconciliation:ledgerco" and res["provider_ref"] == "pi_second" and res["executed"] is True
    assert x.rt.authority.lease(x.lease())["state"] == "settled" and x.jobs.get(x.lease())["state"] == "resolved"


def test_authoritative_negative_releases_re_gates_and_retries_the_action_exactly_once(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, mode="crash_before_effect", probes=[("op.*", absent(), Q_STRICT)])
    rep = tick(x, 31)
    assert rep.resolved and x.rt._missions[x.mid].state == MissionState.SUCCEEDED
    assert second_calls(x) == 1 and x.w.provider.effects == {"first": 1, "second": 1, "third": 1}       # the action ran once, after proof
    assert events(x.rt, x.mid, "ReconciliationResolved")[0]["executed"] is False
    assert x.rt.authority.outstanding() == []


@pytest.mark.parametrize("probe,why", [
    (inconclusive("not found"), "an honest 'not found'"),
    (absent(authoritative=True), "an adapter that is NOT qualified for negatives claiming one"),
])
def test_a_non_authoritative_not_found_retries_the_probe_and_never_the_action(tmp_path, monkeypatch, probe, why):
    x = build(tmp_path, monkeypatch, probes=[("op.*", probe, Q_WEAK)])
    lease = x.lease()
    delays = []
    for _ in range(4):
        before = x.jobs.get(lease)["attempts"]
        rep = tick(x, 400)
        assert rep.retried == [lease] and not rep.escalated and not rep.resolved
        delays.append(x.jobs.get(lease)["next_at"] - x.clock())
        assert x.jobs.get(lease)["attempts"] == before + 1
        assert x.rt._missions[x.mid].state == MissionState.WAITING_RECONCILIATION
        assert second_calls(x) == 0 and x.rt.authority.lease(lease)["state"] in ("dispatched", "unknown")      # untouched
    assert delays == [60.0, 120.0, 240.0, 300.0]                                                       # bounded exponential backoff
    last = tick(x, 400)                                                                               # the 5th probe exhausts the budget
    assert last.escalated == [lease] and x.jobs.get(lease)["state"] == "escalated" and x.jobs.get(lease)["attempts"] == 5
    assert "exhausted" in x.jobs.get(lease)["escalation_reason"]
    assert second_calls(x) == 0 and x.rt.authority.lease(lease)["state"] in ("dispatched", "unknown")   # exhausting NEVER permits a resend
    assert x.rt._missions[x.mid].state == MissionState.WAITING_RECONCILIATION
    for _ in range(3):
        assert tick(x, 10_000).probed == []                                                           # an escalated job is not probed again


def test_a_generic_http_404_is_inconclusive_never_a_confirmed_negative():
    assert from_http_status(404).outcome is O.INCONCLUSIVE
    assert from_http_status(410).outcome is O.INCONCLUSIVE
    assert from_http_status(429, retry_after="17").retry_after_s == 17.0 and from_http_status(429).outcome is O.TRANSIENT_ERROR
    assert from_http_status(503).outcome is O.TRANSIENT_ERROR and from_http_status(408).outcome is O.TRANSIENT_ERROR
    assert from_http_status(403).outcome is O.UNSUPPORTED
    assert from_http_status(429, retry_after="soon").retry_after_s is None


def test_transient_errors_back_off_and_honour_a_bounded_retry_after(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, probes=[("op.*", transient(retry_after=120), Q_STRICT)])
    lease = x.lease()
    tick(x, 31)
    assert x.jobs.get(lease)["next_at"] - x.clock() == 120.0                                          # Retry-After beat the 60s backoff
    x.reg._adapters.clear()
    x.reg.register("op.*", transient(retry_after=10 ** 7), Q_STRICT)
    tick(x, 200)
    assert x.jobs.get(lease)["next_at"] - x.clock() == POLICY.max_retry_after_s                       # ... but is bounded
    assert second_calls(x) == 0


def test_a_probe_that_raises_is_a_transient_failure_not_a_verdict(tmp_path, monkeypatch):
    def boom(ctx):
        raise ConnectionError("provider unreachable")
    x = build(tmp_path, monkeypatch, probes=[("op.*", boom, Q_STRICT)])
    rep = tick(x, 31)
    assert rep.retried and not rep.resolved and "ConnectionError" in x.jobs.get(x.lease())["last_detail"]
    assert second_calls(x) == 0 and x.rt._missions[x.mid].state == MissionState.WAITING_RECONCILIATION


def test_unsupported_escalates_immediately_and_so_does_having_no_adapter_at_all(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, probes=[("op.*", lambda ctx: ProbeResult(O.UNSUPPORTED, detail="provider has no lookup API"), Q_STRICT)])
    rep = tick(x, 31)
    assert rep.escalated == [x.lease()] and x.jobs.get(x.lease())["attempts"] == 1 and "no lookup API" in x.jobs.get(x.lease())["escalation_reason"]
    (tmp_path / "none").mkdir()
    y = build(tmp_path / "none", monkeypatch, probes=[])
    rep = tick(y, 31)
    assert rep.escalated == [y.lease()] and "no reliable lookup" in y.jobs.get(y.lease())["escalation_reason"] and second_calls(y) == 0


def test_conflicting_evidence_escalates_immediately(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, probes=[("op.*", executed(), Q_STRICT), ("op.*", absent(), Q_STRICT)])
    rep = tick(x, 31)
    assert rep.escalated == [x.lease()] and "disagree" in x.jobs.get(x.lease())["escalation_reason"]
    assert x.rt.authority.lease(x.lease())["state"] in ("dispatched", "unknown") and second_calls(x) == 0


def test_a_negative_that_contradicts_a_recorded_provider_reference_escalates(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, probes=[("op.*", absent(), Q_STRICT)])
    x.rt.authority.annotate(x.lease(), provider_ref="pi_seen_earlier")
    rep = tick(x, 31)
    assert rep.escalated == [x.lease()] and "provider reference was recorded" in x.jobs.get(x.lease())["escalation_reason"]
    assert second_calls(x) == 0


def test_positive_evidence_without_provenance_is_not_accepted(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, mode="crash_after_effect", probes=[("op.*", executed(ev=None), Q_STRICT)])
    rep = tick(x, 31)
    assert rep.retried and not rep.resolved and x.rt._missions[x.mid].state == MissionState.WAITING_RECONCILIATION
    assert x.rt.authority.lease(x.lease())["state"] in ("dispatched", "unknown")


def test_confirmed_executed_that_later_steps_need_but_carries_no_result_escalates_without_changing_anything(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, mode="crash_after_effect", probes=[("op.*", executed(result=None), Q_STRICT)])
    rep = tick(x, 31)
    assert rep.escalated == [x.lease()] and "supply the provider's real result" in x.jobs.get(x.lease())["escalation_reason"]
    assert x.rt.authority.lease(x.lease())["state"] in ("dispatched", "unknown")                      # refused before anything changed
    assert x.rt._missions[x.mid].state == MissionState.WAITING_RECONCILIATION


# ── eventual consistency: the adapter declares it; the supervisor enforces it ────────────
def test_an_eventually_consistent_negative_is_not_trusted_inside_the_visibility_window_but_is_after_it(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, probes=[("op.*", absent(), Q_EVENTUAL)])
    lease = x.lease()
    dispatched = x.jobs.get(lease)["dispatched_at"]
    assert x.jobs.get(lease)["next_at"] >= dispatched + 600                                           # no probe before the window can say anything
    x.clock.t = dispatched + 100
    # (judge the same answer directly, to show the rule is enforced at judgement time too, not only by when the probe is scheduled)
    v = x.reg.judge(Q_EVENTUAL, absent()(SimpleNamespace(now=x.clock())), dispatched_at=dispatched, now=x.clock())
    assert v.outcome is O.INCONCLUSIVE and v.downgraded and "visibility window" in v.reason
    x.clock.t = dispatched + 601
    rep = x.sup.tick()
    assert rep.resolved and second_calls(x) == 1 and x.rt._missions[x.mid].state == MissionState.SUCCEEDED


def test_an_eventually_consistent_adapter_with_no_documented_window_can_never_prove_absence(tmp_path, monkeypatch):
    q = Qualification("mystery", eventually_consistent=True, visibility_window_s=None, authoritative_negative=True)
    x = build(tmp_path, monkeypatch, probes=[("op.*", absent(), q)])
    for _ in range(5):
        tick(x, 400)
    j = x.jobs.get(x.lease())
    assert j["state"] == "escalated" and second_calls(x) == 0


def test_a_strongly_consistent_adapter_that_is_not_qualified_for_negatives_cannot_prove_absence(tmp_path, monkeypatch):
    """Isolated from the eventual-consistency rule: this adapter has no visibility problem, it just was never declared authoritative."""
    q = Qualification("honest-but-unqualified", eventually_consistent=False, authoritative_negative=False)
    x = build(tmp_path, monkeypatch, probes=[("op.*", absent(authoritative=True), q)])
    rep = tick(x, 31)
    assert rep.retried and not rep.resolved and second_calls(x) == 0
    assert "not qualified" in x.jobs.get(x.lease())["last_detail"] and x.rt._missions[x.mid].state == MissionState.WAITING_RECONCILIATION


def test_a_negative_must_also_be_marked_authoritative_by_the_adapter_and_carry_evidence():
    reg = ProbeRegistry()
    r = ProbeResult(O.CONFIRMED_NOT_EXECUTED, evidence={"kind": "provider_record", "reference": "search: 0 results"}, authoritative_negative=False)
    assert reg.judge(Q_STRICT, r, dispatched_at=0, now=1e9).outcome is O.INCONCLUSIVE
    r2 = ProbeResult(O.CONFIRMED_NOT_EXECUTED, evidence=None, authoritative_negative=True)
    assert reg.judge(Q_STRICT, r2, dispatched_at=0, now=1e9).outcome is O.INCONCLUSIVE
    r3 = ProbeResult(O.CONFIRMED_NOT_EXECUTED, evidence={"kind": "provider_record", "reference": "search: 0 results"}, authoritative_negative=True)
    assert reg.judge(Q_STRICT, r3, dispatched_at=0, now=1e9).outcome is O.CONFIRMED_NOT_EXECUTED
    assert reg.judge(Q_STRICT, "not a ProbeResult", dispatched_at=0, now=1e9).outcome is O.INCONCLUSIVE


def test_the_first_probe_waits_for_the_documented_horizon(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, probes=[("op.*", inconclusive(), Q_EVENTUAL)])
    j = x.jobs.get(x.lease())
    assert j["next_at"] >= j["dispatched_at"] + 600 > x.clock() + 30


def test_policy_presets_and_jitter_are_bounded():
    assert set(PRESETS) == {"fast", "default", "slow"} and PRESETS["slow"].max_s == 900
    p = RetryPolicy(jitter=0.2)
    rng = random.Random(3)
    for n in range(1, 6):
        base = min(p.max_s, p.initial_s * 2 ** (n - 1))
        for _ in range(50):
            d = p.delay_before(n, rng=rng)
            assert base * 0.8 - 1e-9 <= d <= base * 1.2 + 1e-9
    assert [RetryPolicy(jitter=0).delay_before(n) for n in range(1, 6)] == [30, 60, 120, 240, 300]


# ── durability: restarts, replicas, notifications ────────────────────────────────────────
def test_a_restart_does_not_reset_the_retry_budget(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, probes=[("op.*", inconclusive(), Q_WEAK)])
    lease = x.lease()
    for _ in range(3):
        tick(x, 400)
    assert x.jobs.get(lease)["attempts"] == 3
    rt2 = x.w.restart(x.mid)                                                  # the process dies and comes back
    jobs2 = ReconcileJobs(str(x.tmp / "jobs.db"))
    reg = ProbeRegistry(); reg.register("op.*", inconclusive(), Q_WEAK)
    sup2 = ReconciliationSupervisor(rt2, jobs2, reg, policy=POLICY, owner="sup-2", clock=x.clock, rng=random.Random(2))
    sup2.scan()
    assert jobs2.get(lease)["attempts"] == 3                                   # not reset by the rescan
    x.clock.advance(400); sup2.tick()
    x.clock.advance(400); rep = sup2.tick()
    assert rep.escalated == [lease] and jobs2.get(lease)["attempts"] == 5     # 5 probes TOTAL across both processes, not 8
    assert second_calls(x) == 0


def test_the_escalation_notification_is_sent_once_across_restarts_and_replicas(tmp_path, monkeypatch):
    sent = []
    x = build(tmp_path, monkeypatch, probes=[("op.*", lambda c: ProbeResult(O.UNSUPPORTED, detail="no lookup"), Q_STRICT)],
              notifier=lambda job, key: sent.append((job["job_id"], key)))
    rep = tick(x, 31)
    assert rep.escalated and len(sent) == 1 and rep.notified == [x.lease()]
    for i in range(3):                                                         # more ticks, a restart, a second replica
        tick(x, 1000)
    rt2 = x.w.restart(x.mid)
    reg = ProbeRegistry()
    sup2 = ReconciliationSupervisor(rt2, ReconcileJobs(str(x.tmp / "jobs.db")), reg, policy=POLICY, owner="sup-2", clock=x.clock,
                                    notifier=lambda job, key: sent.append((job["job_id"], key)))
    sup2.tick(); sup2.tick()
    assert len(sent) == 1 and sent[0][1].startswith(f"escalation:{x.lease()}:")                  # exactly once, with a stable key
    assert x.jobs.get(x.lease())["notify_state"] == "sent" and x.jobs.get(x.lease())["notified_at"]


def test_a_failing_notifier_is_retried_then_succeeds_once_and_a_dead_one_is_visible(tmp_path, monkeypatch):
    attempts, sent = [], []

    def flaky(job, key):
        attempts.append(key)
        if len(attempts) < 3:
            raise ConnectionError("pager is down")
        sent.append(key)
    x = build(tmp_path, monkeypatch, probes=[("op.*", lambda c: ProbeResult(O.UNSUPPORTED), Q_STRICT)], notifier=flaky)
    tick(x, 31)
    for _ in range(4):
        tick(x, 100)
    assert len(sent) == 1 and len(attempts) == 3 and len(set(attempts)) == 1                    # the same idempotency key every time
    assert x.jobs.get(x.lease())["notify_state"] == "sent"
    (tmp_path / "dead").mkdir()
    y = build(tmp_path / "dead", monkeypatch, probes=[("op.*", lambda c: ProbeResult(O.UNSUPPORTED), Q_STRICT)],
              notifier=lambda j, k: (_ for _ in ()).throw(ConnectionError("down")))
    tick(y, 31)
    for _ in range(8):
        tick(y, 100)
    assert y.jobs.get(y.lease())["notify_state"] == "failed" and y.jobs.get(y.lease())["state"] == "escalated"     # visible, not silent


def test_jobs_are_claimed_by_exactly_one_replica_and_a_stale_claimant_cannot_record(tmp_path):
    jobs = ReconcileJobs(str(tmp_path / "j.db"))
    for i in range(6):
        jobs.enqueue(f"L{i}", mission_id="m", node_id=f"n{i}", capability="op.x", first_at=0, max_attempts=5, now=0)
    got, barrier = [], threading.Barrier(8)

    def worker(name):
        barrier.wait()
        got.extend((name, j["job_id"], j["claim_token"]) for j in jobs.claim_due(name, 100, claim_s=50, limit=6))
    ts = [threading.Thread(target=worker, args=(f"r{i}",)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(g[1] for g in got) == [f"L{i}" for i in range(6)]                              # each job claimed by exactly one replica
    owner, jid, token = got[0]
    assert jobs.claim_due("late", 120, claim_s=50) == []                                        # claimed jobs are not reclaimable yet
    again = jobs.claim_due("late", 200, claim_s=50)                                             # the claim expired: a crashed worker's jobs return
    assert jid in [j["job_id"] for j in again]
    assert jobs.record(jid, owner, token, outcome="inconclusive", detail="x", now=201, next_at=300)["ok"] is False   # the stale claimant is fenced out
    new = next(j for j in again if j["job_id"] == jid)
    assert jobs.record(jid, "late", new["claim_token"], outcome="inconclusive", detail="x", now=201, next_at=300)["ok"] is True


def test_a_person_resolving_the_mission_closes_the_job_and_a_reopened_job_gets_a_fresh_budget(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, probes=[("op.*", inconclusive(), Q_WEAK)])
    lease = x.lease()
    for _ in range(5):
        tick(x, 400)
    assert x.jobs.get(lease)["state"] == "escalated"
    assert x.jobs.requeue(lease, now=x.clock(), first_at=x.clock() + 30) and x.jobs.get(lease)["attempts"] == 0 and x.jobs.get(lease)["state"] == "pending"
    x.rt.reconcile_node(x.mid, x.rt.repo.pending_reconciliation(x.mid)[0]["node_id"], executed=False, evidence=EVIDENCE, actor="admin",
                        reason="provider dashboard shows nothing for this key")
    x.sup.scan()
    assert x.jobs.get(lease)["state"] == "resolved" and x.jobs.get(lease)["resolution"] == "resolved_by_a_person"
    assert tick(x, 10_000).probed == []


def test_the_job_history_records_every_probe_for_audit(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, probes=[("op.*", inconclusive(), Q_WEAK)])
    for _ in range(5):
        tick(x, 400)
    kinds = [h["kind"] for h in x.jobs.history(x.lease())]
    assert kinds == ["enqueued", "probed", "probed", "probed", "probed", "escalated"]


# ── mission ownership and fencing ────────────────────────────────────────────────────────
def test_only_one_replica_can_own_a_mission_and_every_takeover_advances_the_fence(tmp_path):
    clock = Clock()
    L = MissionLeases(str(tmp_path / "l.db"), clock=clock, ttl_s=60, heartbeat=False)
    a = L.acquire("m1", "A")
    assert a and a.token == 1 and L.acquire("m1", "B") is None
    assert L.acquire("m1", "A").token == 1                                    # the same owner re-entering keeps its token
    clock.advance(61)                                                         # A stalls past its ttl
    b = L.acquire("m1", "B")
    assert b and b.token == 2 and L.acquire("m1", "A") is None
    b.release()
    c = L.acquire("m1", "C")
    assert c.token == 3                                                       # a release never moves the fence backwards


def test_racing_acquirers_produce_exactly_one_owner(tmp_path):
    L = MissionLeases(str(tmp_path / "l.db"), ttl_s=60, heartbeat=False)
    wins, barrier = [], threading.Barrier(10)

    def go(i):
        barrier.wait()
        l = L.acquire("m1", f"r{i}")
        if l:
            wins.append(l.token)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins == [1]


def test_a_stale_writer_cannot_append_after_a_takeover(tmp_path):
    clock = Clock()
    L = MissionLeases(str(tmp_path / "l.db"), clock=clock, ttl_s=60, heartbeat=False)
    log = EventStore(str(tmp_path / "events.jsonl"))
    store = FencedEventStore(log, L)
    a = L.acquire("m1", "A")
    store.append("StepA", "m1", {"by": "A"})
    clock.advance(61)
    b = L.acquire("m1", "B")                                                  # A stalled; B takes over
    # A is a SEPARATE process in reality; here A's view still believes it owns the mission. Its write must be refused.
    L.held["m1"] = a                                                          # A "wakes up" and tries to continue
    with pytest.raises(FencingError):
        store.append("StaleWrite", "m1", {"by": "A"})
    L.held["m1"] = b
    store.append("StepB", "m1", {"by": "B"})
    assert [e.type for e in log.for_mission("m1")] == ["StepA", "StepB"]       # the stale write is not in the log


def test_the_check_and_the_write_are_one_critical_section(tmp_path):
    """A writer that passed the check cannot be overtaken: a takeover waits for the in-flight append, so there is no window."""
    clock = Clock()
    L = MissionLeases(str(tmp_path / "l.db"), clock=clock, ttl_s=60, heartbeat=False)
    a = L.acquire("m1", "A")
    entered, release, result = threading.Event(), threading.Event(), {}

    def slow_write():
        with L.guard("m1", a.token):
            entered.set()
            release.wait(5)

    t = threading.Thread(target=slow_write)
    t.start()
    assert entered.wait(5)
    clock.advance(61)

    def takeover():
        result["b"] = L.acquire("m1", "B")                                    # must block until the in-flight write finishes
    t2 = threading.Thread(target=takeover)
    t2.start()
    time.sleep(0.3)
    assert "b" not in result                                                  # still waiting: it cannot interleave with the write
    release.set()
    t.join(); t2.join()
    assert result["b"].token == 2


def test_never_leased_missions_pass_through_and_strict_mode_requires_a_lease(tmp_path):
    L = MissionLeases(str(tmp_path / "l.db"), heartbeat=False)
    log = EventStore(str(tmp_path / "e.jsonl"))
    FencedEventStore(log, L).append("X", "unmanaged", {})
    assert [e.type for e in log.for_mission("unmanaged")] == ["X"]
    with pytest.raises(FencingError):
        FencedEventStore(log, L, strict=True).append("X", "unmanaged2", {})
    managed = FencedEventStore(log, L)
    L.acquire("m1", "A").release()
    with pytest.raises(FencingError):                                          # once managed, a writer without the lease is refused
        managed.append("X", "m1", {})


def test_the_heartbeat_keeps_a_live_owner_and_a_lost_lease_stops_writes(tmp_path):
    db = str(tmp_path / "l.db")
    La, Lb = MissionLeases(db, ttl_s=0.6), MissionLeases(db, ttl_s=0.6)         # two processes: separate handles over one database
    log = EventStore(str(tmp_path / "e.jsonl"))
    store = FencedEventStore(log, La)
    with La.acquire("m1", "A") as lease:
        time.sleep(1.5)                                                       # more than twice the ttl: only the heartbeat kept it
        store.append("StillMine", "m1", {})
        assert not lease.lost
    assert La.current("m1")["owner"] is None
    lost = La.acquire("m1", "A", ttl_s=0.2)                                   # no heartbeat: it expires, someone else takes over
    time.sleep(0.5)
    assert Lb.acquire("m1", "B", ttl_s=30)
    assert lost.renew() is False and lost.lost
    with pytest.raises(FencingError):
        store.append("TooLate", "m1", {})                                     # A's process knows it lost, and so does the database


def test_a_driver_that_loses_the_mission_mid_run_is_stopped_and_the_new_owner_continues(tmp_path, monkeypatch):
    """The fence at work through a real runtime: A stalls inside a step, B takes over, A's next write is refused."""
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "test")
    clock = Clock()
    La = MissionLeases(str(tmp_path / "l.db"), clock=clock, ttl_s=60, heartbeat=False)
    Lb = MissionLeases(str(tmp_path / "l.db"), clock=clock, ttl_s=60, heartbeat=False)    # replica B: its own handle, same database
    prov = Provider()
    h = handlers(prov, "ok")
    state = {}
    first = h["op.first"]

    def stalling_first(i):
        r = first(i)
        clock.advance(61)                                                     # A stalls inside the step ...
        state["b"] = Lb.acquire(state["mid"], "B")                            # ... and B takes the mission over
        return r
    h["op.first"] = stalling_first
    from agentic_os.mission.executor import Executor, InMemoryOperatorClient
    log = EventStore(str(tmp_path / "events.jsonl"))
    rt = MissionRuntime(registry(), Executor(InMemoryOperatorClient(h)), store=FencedEventStore(log, La), planner=Plan(chain=True))
    m = rt.create_mission("three steps", policy_refs=["*"], tenant="acme", principal="o")
    state["mid"] = m.id
    a = La.acquire(m.id, "A")
    with pytest.raises(FencingError):
        rt.run(m.id)
    assert state["b"].token == 2 and prov.calls == ["first"]                  # A got no further than the step it was in
    stale = [e for e in log.for_mission(m.id) if e.type in ("NodeSucceeded",)]
    assert stale == []                                                        # A's result was NOT recorded after it lost ownership


# ── independent progress, and not mistaking a wait for a stall ───────────────────────────
def test_independent_work_continues_while_one_node_waits_and_runs_exactly_once(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, chain=False, probes=[("op.*", inconclusive(), Q_WEAK)])
    assert x.w.provider.calls == ["third"]                                                    # the independent node ran past the unknown one
    assert x.rt._missions[x.mid].state == MissionState.WAITING_RECONCILIATION and second_calls(x) == 0
    before = len(x.rt.store.for_mission(x.mid))
    for _ in range(3):
        assert x.rt.run(x.mid).state == MissionState.WAITING_RECONCILIATION
    rt2 = x.w.restart(x.mid)
    assert rt2.run(x.mid).state == MissionState.WAITING_RECONCILIATION
    assert x.w.provider.calls == ["third"] and len(x.rt.store.for_mission(x.mid)) == before     # nothing reran, nothing was written
    x.rt.reconcile_node(x.mid, x.rt.repo.pending_reconciliation(x.mid)[0]["node_id"], executed=False, evidence=EVIDENCE,
                        actor="admin", reason="provider shows nothing for this key")
    assert x.rt._missions[x.mid].state == MissionState.SUCCEEDED and x.w.provider.calls == ["third", "second"]


def test_independent_work_across_later_waves_does_not_keep_re_gating_the_unknown_node(tmp_path, monkeypatch):
    """first, second (unknown), third (independent), fourth (needs third): fourth runs in a LATER wave. The unknown node must not be
    re-gated and re-refused on that wave, or the log fills with duplicate refusals for as long as independent work continues."""
    x = build(tmp_path, monkeypatch, chain=False, deep=True, probes=[("op.*", inconclusive(), Q_WEAK)])
    assert x.w.provider.calls == ["third", "fourth"] and second_calls(x) == 0
    assert x.rt._missions[x.mid].state == MissionState.WAITING_RECONCILIATION
    assert len(events(x.rt, x.mid, "RedispatchRefused")) == 1 and len(events(x.rt, x.mid, "ReconciliationRequired")) == 1


def test_dependents_of_an_unknown_node_stay_blocked_until_it_is_reconciled(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, chain=True, probes=[("op.*", inconclusive(), Q_WEAK)])
    assert x.w.provider.calls == [] and x.rt._missions[x.mid].state == MissionState.WAITING_RECONCILIATION
    x.rt.reconcile_node(x.mid, x.rt.repo.pending_reconciliation(x.mid)[0]["node_id"], executed=False, evidence=EVIDENCE, actor="admin",
                        reason="provider shows nothing for this key")
    assert x.w.provider.calls == ["second", "third"]


def test_blocking_is_transitive_and_leaves_unrelated_nodes_alone():
    from agentic_os.mission.runtime import MissionRuntime as R
    n = lambda i, deps=(): SimpleNamespace(id=i, depends_on=list(deps))
    graph = SimpleNamespace(nodes=[n("a"), n("b", ["a"]), n("c", ["b"]), n("d", ["c"]), n("x"), n("y", ["x"]), n("z", ["a", "x"])])
    blocked = R._reconciliation_blocked(None, graph, [{"node_id": "b"}])
    assert blocked == {"b", "c", "d"}                                                         # b and everything downstream of it
    assert R._reconciliation_blocked(None, graph, [{"node_id": "a"}]) == {"a", "b", "c", "d", "z"}   # z waits on a too
    assert R._reconciliation_blocked(None, graph, []) == set()


def test_a_waiting_mission_is_never_classified_as_stalled(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, probes=[("op.*", inconclusive(), Q_WEAK)])
    far = x.clock() + 10 ** 6                                                                 # a very long time later
    a = classify(x.rt, x.mid, x.jobs, now=far)
    assert a.kind is Wait.WAITING_OBSERVATION and not a.watchdog_eligible and not a.needs_a_person and a.next_probe_at
    for _ in range(5):
        tick(x, 400)
    a = classify(x.rt, x.mid, x.jobs, now=far)
    assert a.kind is Wait.WAITING_ESCALATED and a.needs_a_person and not a.watchdog_eligible           # a person is needed; do NOT replan
    assert "unresolved after 5 probe" in a.detail
    x.rt.reconcile_node(x.mid, x.rt.repo.pending_reconciliation(x.mid)[0]["node_id"], executed=False, evidence=EVIDENCE, actor="admin",
                        reason="provider shows nothing for this key")
    assert classify(x.rt, x.mid, x.jobs, now=far).kind is Wait.TERMINAL


def test_only_a_genuinely_hung_running_mission_is_eligible_for_the_watchdog(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt = w.boot("ok")
    m = w.new_mission(rt)
    rt._set_state(rt._missions[m.id], MissionState.RUNNING)                                   # started, then nothing recorded
    last = rt.repo.timeline(m.id)[-1]["ts"]
    assert classify(rt, m.id, now=last + 60, hung_after_s=900).kind is Wait.ACTIVE
    hung = classify(rt, m.id, now=last + 901, hung_after_s=900)
    assert hung.kind is Wait.HUNG and hung.watchdog_eligible
    rt._set_state(rt._missions[m.id], MissionState.PAUSED)
    assert classify(rt, m.id, now=last + 10 ** 6).kind is Wait.PAUSED


def test_a_mission_waiting_for_a_person_is_not_stalled_either(tmp_path, monkeypatch):
    from agentic_os.mission.policy import Effect, MissionPolicy, PolicyRule
    w = World(tmp_path, monkeypatch, guard=False)
    rt = w.boot("ok")
    pol = MissionPolicy(id="p", version="1", rules=(PolicyRule("w", Effect.REQUIRE_APPROVAL, "side_effecting"),))
    m = rt.create_mission("needs approval", policy_refs=["*"], policy=pol)
    assert rt.run(m.id).state == MissionState.WAITING_HUMAN
    a = classify(rt, m.id, now=time.time() + 10 ** 6)
    assert a.kind is Wait.WAITING_HUMAN and a.needs_a_person and not a.watchdog_eligible


# ── the supervisor and the mission lease together ────────────────────────────────────────
def test_a_replica_that_does_not_own_the_mission_skips_it_without_using_an_attempt(tmp_path, monkeypatch):
    clock = Clock()
    leases = MissionLeases(str(tmp_path / "l.db"), clock=clock, ttl_s=600, heartbeat=False)
    x = build(tmp_path, monkeypatch, mode="crash_after_effect", probes=[("op.*", executed(), Q_STRICT)], leases=leases)
    leases.clock = x.clock
    other = leases.acquire(x.mid, "someone-else")                                            # another replica is driving this mission
    rep = tick(x, 31)
    assert rep.skipped == [x.lease()] and x.jobs.get(x.lease())["attempts"] == 0 and x.jobs.get(x.lease())["state"] == "pending"
    assert x.rt._missions[x.mid].state == MissionState.WAITING_RECONCILIATION
    other.release()
    rep = tick(x, 100)
    assert rep.resolved and x.rt._missions[x.mid].state == MissionState.SUCCEEDED


def test_events_for_sentinel_are_emitted_for_probes_retries_and_escalation(tmp_path, monkeypatch):
    seen = []
    x = build(tmp_path, monkeypatch, probes=[("op.*", inconclusive(), Q_WEAK)])
    x.sup.on_event = seen.append
    for _ in range(5):
        tick(x, 400)
    kinds = [e["type"] for e in seen]
    assert kinds.count("RECONCILE_PROBED") == 5 and kinds.count("RECONCILE_RETRY_SCHEDULED") == 4 and kinds.count("UNKNOWN_OUTCOME") == 1
    esc = next(e for e in seen if e["type"] == "UNKNOWN_OUTCOME")
    assert esc["escalation"] == "exhausted" and esc["capability"] == "op.second" and esc["job_id"] == x.lease()
