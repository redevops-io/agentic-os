"""The goal-loop controller: hard ceilings stop it, soft thresholds make it diagnose -> replan -> escalate, only an independent verifier
completes a goal, uncertainty and authority are never planned around, and every restart, cancel and stale writer is safe.

Each iteration is a real mission on a real runtime (and, where it matters, a durable authority ledger): the loop adds no new way to
execute, so these tests also pin that it cannot become one."""
from __future__ import annotations

import threading
import time

import pytest

from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, SqliteEnvelopeStore, SqliteLedger)
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.goal_loop import (
    GoalLoop, HardLimits, IterationContext, LoopBusy, LoopError, LoopSpec, SoftThresholds, Verification, _sha, classify_loop, fold_loop)
from agentic_os.mission.ownership import FencedEventStore, FencingError, MissionLeases
from agentic_os.mission.policy import STANDING, Effect, MissionPolicy, PolicyRule
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import (
    Budget, CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState, NodeCost)
from agentic_os.mission.waitstate import Wait

NOW = time.time()
EVIDENCE = {"kind": "provider_record", "reference": "provider dashboard, record ch_b, 2026-10-24 09:14"}
VERIFY_EV = {"kind": "provider_record", "reference": "crm: onboarding state is 'complete' for acct 42"}
CAPS = "abcdef"


class Provider:
    """The EXTERNAL system: counts every effect per capability. `mode[cap]`: ok | fail (clean, nothing applied) | timeout (applied, then
    the caller sees a timeout: UNKNOWN)."""

    def __init__(self):
        self.delay: dict = {}
        self.effects: dict = {}
        self.calls: list = []
        self.mode: dict = {}
        self.gate: dict = {}
        self.lock = threading.Lock()

    def handler(self, cap):
        def fn(inputs):
            m = self.mode.get(cap, "ok")
            with self.lock:
                self.calls.append(cap)
            g = self.gate.get(cap)
            if g is not None:
                g.wait(10)
            if self.delay.get(cap):
                time.sleep(self.delay[cap])
            if m == "fail":
                e = RuntimeError(f"{cap} rejected before sending")
                e.not_applied = True
                raise e
            with self.lock:
                self.effects[cap] = self.effects.get(cap, 0) + 1
            if m == "timeout":
                raise TimeoutError("timed out after the request was written")
            return {"ok": cap, "provider_object_id": f"obj_{cap}"}
        return fn


def registry(usd=0.5):
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [
        *[CapabilitySpec(f"op.{c}", "op", provides=[f"done_{c}"], side_effecting=True, undo="op.undo", cost=NodeCost(usd=usd)) for c in CAPS],
        CapabilitySpec("op.undo", "op", side_effecting=True)]))
    return reg


class StepsPlanner:
    """The plan IS the list of steps named after the '|' in the goal, so a restart replays exactly the plan that ran."""

    def plan(self, mission_id, goal, context):
        spec = goal.split("|", 1)[1] if "|" in goal else ""
        chain = ">" in spec                                                 # "a>b": b needs what a produced; "a,b": independent
        names = [s for s in spec.replace(">", ",").split(",") if s]
        return ExecutionIntent(mission_id=mission_id, steps=[
            IntentStep(outcome=f"done_{c}", need=c, inputs_from=[f"done_{names[i - 1]}"] if chain and i else []) for i, c in enumerate(names)])


def sig(cap) -> str:
    return f"op.{cap}|done_{cap}|{_sha({})}"


class Driver:
    """`steps_for(ctx)` says which steps iteration `ctx.n` runs; `alternatives` are the replans' strategies (None = no other approach)."""

    def __init__(self, rt, steps_for, alternatives=None, **mission_kw):
        self.rt, self.steps_for, self.alternatives, self.kw = rt, steps_for, alternatives, mission_kw
        self.replans: list = []
        self.started: list = []
        self.sep = ","

    def start(self, ctx: IterationContext) -> str:
        steps = self.steps_for(ctx)
        self.started.append((ctx.n, tuple(steps)))
        m = self.rt.create_mission(f"{ctx.goal}|{self.sep.join(steps)}", policy_refs=["*"], **self.kw)
        return m.id

    def replan(self, ctx: IterationContext):
        self.replans.append(ctx.diagnosis["kind"])
        if self.alternatives is None or len(self.replans) > len(self.alternatives):
            return None
        return {"strategy": self.alternatives[len(self.replans) - 1]}


class Lab:
    def __init__(self, tmp, monkeypatch, *, profile="test", guard=False, fenced=False, clock=None):
        monkeypatch.setenv("AGENTIC_OS_PROFILE", profile)
        monkeypatch.delenv("AGENTIC_OS_APPROVAL_SECRET", raising=False)
        self.tmp, self.provider, self.guard_on, self.fenced = tmp, Provider(), guard, fenced
        self.log, self.loop_log, self.db = str(tmp / "missions.jsonl"), str(tmp / "loops.jsonl"), str(tmp / "auth.db")
        self.clock = clock or time.time
        self.events: list = []
        if guard:
            SqliteEnvelopeStore(self.db).put(AuthorityEnvelope(
                tenant="acme", principal="o", mission_id="env", issued_at=NOW - 10, expires_at=NOW + 100_000,
                grants=(Grant("op.*"),), budget=AutonomyBudget(usd=1000, max_actions=1000)))
        self.leases = MissionLeases(str(tmp / "leases.db"), clock=self.clock, ttl_s=60, heartbeat=False) if fenced else None

    def runtime(self, budget_usd=100.0) -> MissionRuntime:
        store = EventStore(self.log)
        if self.fenced:
            store = FencedEventStore(store, self.leases)
        kw = {}
        if self.guard_on:
            kw = dict(authority=AuthorityGuard(SqliteEnvelopeStore(self.db), SqliteLedger(self.db + ".usage")), authority_key=lambda m: "env")
        reg = registry()
        return MissionRuntime(reg, Executor(InMemoryOperatorClient({f"op.{c}": self.provider.handler(c) for c in CAPS} | {"op.undo": lambda i: {}})),
                              store=store, planner=StepsPlanner(), **kw)

    def mission_kw(self):
        if not self.guard_on:
            return {}
        pol = MissionPolicy(id="p", version="1", rules=(PolicyRule("w", Effect.REQUIRE_APPROVAL, "side_effecting", approval_mode=STANDING),))
        return dict(tenant="acme", principal="o", policy=pol)

    def loop(self, rt, driver, *, verifier=None, leases="auto", clock=None, owner="ctl-1") -> GoalLoop:
        return GoalLoop(rt, driver, store=EventStore(self.loop_log), verifier=verifier, on_event=self.events.append,
                        leases=self.leases if leases == "auto" else leases, clock=clock or self.clock, owner=owner)

    def kinds(self):
        return [e["type"] for e in self.events]


def verified(ctx, m):
    return Verification(True, dict(VERIFY_EV), "state is complete")


def never(ctx, m):
    return Verification(False, None, "the account is not yet complete")


def lab(tmp_path, monkeypatch, **kw):
    return Lab(tmp_path, monkeypatch, **kw)


def types(loop, lid):
    return [e.type for e in loop._raw.for_mission(lid)]


# ───────────────────────── verified completion ─────────────────────────
def test_the_goal_completes_only_when_an_independent_verifier_confirms_with_evidence(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    d = Driver(rt, lambda c: ["a", "b"])
    loop = L.loop(rt, d, verifier=verified)
    lid = loop.start(LoopSpec("onboard acct 42"))
    st = loop.run(lid)
    assert st.status == "completed" and st.verified["evidence"]["kind"] == "provider_record" and len(st.iterations) == 1
    assert L.provider.effects == {"a": 1, "b": 1}
    assert "LOOP_COMPLETED" in L.kinds()


def test_without_a_verifier_a_succeeded_mission_is_never_a_verified_goal(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["a"]), verifier=None)
    st = loop.run(loop.start(LoopSpec("g")))
    assert st.status == "completed_unverified" and st.verified is None
    assert st.closed["verified"] is False


@pytest.mark.parametrize("bad", [
    Verification(True, None, "trust me"),                                  # no evidence
    Verification(True, {"kind": "provider_record", "reference": "x"}, ""),  # evidence that points at nothing checkable
    Verification(False, dict(VERIFY_EV), "no"),
])
def test_a_verification_without_usable_evidence_or_that_says_no_does_not_complete_the_goal(tmp_path, monkeypatch, bad):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["a"]), verifier=lambda c, m: bad)
    st = loop.run(loop.start(LoopSpec("g", limits=HardLimits(max_iterations=2))))
    assert st.status != "completed" and st.verified is None
    assert [i.outcome for i in st.iterations if i.finished][0] == "unverified"


def test_a_verifier_that_raises_is_not_a_verification(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()

    def boom(c, m):
        raise RuntimeError("crm is down")
    loop = L.loop(rt, Driver(rt, lambda c: ["a"]), verifier=boom)
    st = loop.run(loop.start(LoopSpec("g", limits=HardLimits(max_iterations=1))))
    assert st.status == "limit" and st.verified is None


def test_a_later_iteration_completes_the_goal_without_redoing_finished_steps(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    seen = {"n": 0}

    def verify(ctx, m):
        seen["n"] += 1
        return verified(ctx, m) if seen["n"] >= 2 else never(ctx, m)

    def steps(ctx):                                                         # the driver uses ctx.completed to skip finished work
        return [c for c in ("a", "b") if sig(c) not in ctx.completed]
    loop = L.loop(rt, Driver(rt, steps), verifier=verify)
    st = loop.run(loop.start(LoopSpec("g", soft=SoftThresholds(stall_iterations=9, repeat_failures=9, plan_repeats=9))))
    assert st.status == "completed"
    assert L.provider.effects == {"a": 1, "b": 1}                           # nothing was done twice across iterations


# ───────────────────────── hard limits are ceilings ─────────────────────────
HIGH = SoftThresholds(stall_iterations=99, repeat_failures=99, plan_repeats=99)


def test_the_iteration_ceiling_stops_the_loop_and_only_the_owner_can_raise_it(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    d = Driver(rt, lambda c: ["a"] if c.n == 1 else [])
    loop = L.loop(rt, d, verifier=never)
    lid = loop.start(LoopSpec("g", limits=HardLimits(max_iterations=3), soft=HIGH))
    st = loop.run(lid)
    assert st.status == "limit" and st.limit_block["limit"] == "iterations" and len(st.iterations) == 3
    assert "LOOP_LIMIT_REACHED" in L.kinds()
    assert loop.run(lid).status == "limit" and len(loop.state(lid).iterations) == 3          # running again changes nothing
    with pytest.raises(LoopError):
        loop.extend_limits(lid, actor="", reason="we need more attempts", max_iterations=5)
    with pytest.raises(LoopError):
        loop.extend_limits(lid, actor="owner@acme", reason="we need more attempts", max_iterations=2)    # a ceiling only goes up
    st = loop.extend_limits(lid, actor="owner@acme", reason="we need more attempts", max_iterations=4)
    assert len(st.iterations) == 4 and st.status == "limit"
    ext = [e.payload for e in loop._raw.for_mission(lid) if e.type == "LimitsExtended"]
    assert ext[0]["actor"] == "owner@acme" and ext[0]["limits"] == {"max_iterations": 4}


def test_usd_and_token_ceilings_stop_before_another_iteration_is_started(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    d = Driver(rt, lambda c: ["a", "b"])                                    # $1.00 per iteration
    loop = L.loop(rt, d, verifier=never)
    lid = loop.start(LoopSpec("g", limits=HardLimits(max_iterations=50, max_usd=2.0), soft=HIGH))
    st = loop.run(lid)
    assert st.status == "limit" and st.limit_block["limit"] == "usd"
    assert len(st.iterations) == 2 and sum(i.usd for i in st.iterations) == 2.0           # no headroom left at the cap: no third iteration


def test_the_active_time_ceiling_is_durable_across_a_restart(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    ticks = iter(range(0, 10_000))
    loop = L.loop(rt, Driver(rt, lambda c: ["a"] if c.n == 1 else []), verifier=never)
    lid = loop.start(LoopSpec("g", limits=HardLimits(max_iterations=50, max_active_s=0.05), soft=HIGH))
    real = rt.run

    def slow(mid):
        time.sleep(0.06)
        return real(mid)
    rt.run = slow
    assert loop.run(lid).status == "limit"
    rt2 = L.runtime()
    loop2 = L.loop(rt2, Driver(rt2, lambda c: []), verifier=never)
    st = loop2.run(lid)
    assert st.status == "limit" and st.limit_block["limit"] == "active_s" and len(st.iterations) <= 2     # a restart did not reset the budget


def test_the_deadline_is_a_ceiling(tmp_path, monkeypatch):
    clock = [NOW]
    L = lab(tmp_path, monkeypatch, clock=lambda: clock[0])
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["a"]), verifier=never)
    lid = loop.start(LoopSpec("g", limits=HardLimits(max_iterations=50, deadline=NOW + 10), soft=HIGH))
    clock[0] = NOW + 11
    st = loop.run(lid)
    assert st.status == "limit" and st.limit_block["limit"] == "deadline" and st.iterations == []


def test_a_loop_cannot_be_started_without_a_ceiling(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: []))
    for bad in (HardLimits(max_iterations=0), HardLimits(max_usd=0), HardLimits(max_active_s=-1)):
        with pytest.raises(LoopError):
            loop.start(LoopSpec("g", limits=bad))
    with pytest.raises(LoopError):
        loop.start(LoopSpec("  "))


def test_budget_near_limit_is_raised_once_before_the_ceiling(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["a", "b"]), verifier=never)
    lid = loop.start(LoopSpec("g", limits=HardLimits(max_iterations=5), soft=HIGH))
    loop.run(lid)
    near = [e for e in L.events if e["type"] == "BUDGET_NEAR_LIMIT"]
    assert len([e for e in near if e["cap"] == "iterations"]) == 1                           # raised once, not on every pass
    assert [e.payload["cap"] for e in loop._raw.for_mission(lid) if e.type == "BudgetNearLimit"].count("iterations") == 1
    assert loop.state(lid).status == "limit"


# ───────────────────────── soft thresholds: diagnose -> replan -> escalate ─────────────────────────
def test_a_repeating_failure_is_diagnosed_and_replanned_not_terminated(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    L.provider.mode["c"] = "fail"
    rt = L.runtime()
    d = Driver(rt, lambda c: ["a", "c"] if not c.guidance else ["a", "d"], alternatives=["use d instead of c"])
    loop = L.loop(rt, d, verifier=verified)
    lid = loop.start(LoopSpec("g", soft=SoftThresholds(repeat_failures=2, stall_iterations=99, plan_repeats=99)))
    st = loop.run(lid)
    assert st.status == "completed" and st.replans == 1
    assert d.replans == ["repeating_failure"]
    t = types(loop, lid)
    assert t.index("Diagnosed") < t.index("Replanned") < t.index("GoalVerified")
    assert [i.outcome for i in st.iterations] == ["failed", "failed", "succeeded"]
    assert "LOOP_STALLED" in L.kinds()
    assert st.guidance == [{"strategy": "use d instead of c"}]


def test_when_replanning_is_exhausted_the_loop_escalates_and_stays_resumable(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    L.provider.mode["c"] = "fail"
    rt = L.runtime()
    d = Driver(rt, lambda c: ["c"], alternatives=["same idea, reworded"])
    loop = L.loop(rt, d, verifier=verified)
    lid = loop.start(LoopSpec("g", soft=SoftThresholds(repeat_failures=2, stall_iterations=99, plan_repeats=99, max_replans=1)))
    st = loop.run(lid)
    assert st.status == "escalated" and st.escalation["kind"] == "repeating_failure" and st.replans == 1
    assert "LOOP_ESCALATED" in L.kinds()
    n = len(st.iterations)
    assert loop.run(lid).status == "escalated" and len(loop.state(lid).iterations) == n           # it waits; it does not grind on
    # the person fixes the cause and lets it continue with guidance
    L.provider.mode["c"] = "ok"
    st = loop.resolve_escalation(lid, actor="ops@acme", decision="continue", reason="the c endpoint is fixed", guidance={"note": "retry c"})
    assert st.status == "completed" and st.guidance[-1] == {"note": "retry c"}
    esc = [e.payload for e in loop._raw.for_mission(lid) if e.type == "EscalationResolved"]
    assert esc[0]["actor"] == "ops@acme"


def test_an_escalation_can_be_stopped_and_needs_a_named_person_and_a_reason(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    L.provider.mode["c"] = "fail"
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["c"]), verifier=verified)
    lid = loop.start(LoopSpec("g", soft=SoftThresholds(repeat_failures=2, max_replans=0)))
    assert loop.run(lid).status == "escalated"
    for kw in (dict(actor=" ", reason="not worth pursuing"), dict(actor="ops", reason="x"), dict(actor="ops", reason="not worth pursuing", decision="maybe"),
               dict(actor=" ", reason="the endpoint is fixed", decision="continue"), dict(actor="ops", reason="fix", decision="continue")):
        with pytest.raises(LoopError):
            loop.resolve_escalation(lid, **{"decision": "stop", **kw})
    assert loop.state(lid).status == "escalated"
    st = loop.resolve_escalation(lid, actor="ops@acme", decision="stop", reason="not worth pursuing")
    assert st.status == "cancelled"
    with pytest.raises(LoopError):
        loop.resolve_escalation(lid, actor="ops@acme", decision="continue", reason="changed my mind")


def test_no_new_progress_for_several_iterations_is_a_stall(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    fail_caps = ["c", "d", "e", "f"]                                        # a different failure each time: not a repeat, still no progress

    def steps(ctx):
        L.provider.mode = {fail_caps[(ctx.n - 1) % 4]: "fail"}
        return [fail_caps[(ctx.n - 1) % 4]]
    d = Driver(rt, steps)
    loop = L.loop(rt, d, verifier=verified)
    lid = loop.start(LoopSpec("g", soft=SoftThresholds(repeat_failures=99, stall_iterations=3, plan_repeats=99, max_replans=0)))
    st = loop.run(lid)
    assert st.status == "escalated" and st.escalation["kind"] == "no_progress" and len(st.iterations) == 3
    assert [e.payload["kind"] for e in loop._raw.for_mission(lid) if e.type == "Diagnosed"] == ["no_progress"]


def test_progress_resets_the_stall_counter(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    order = ["c", "a", "d", "b", "e"]                                       # every other iteration completes a NEW step before failing

    def steps(ctx):
        cap = order[ctx.n - 1]
        L.provider.mode = {cap: "fail"} if cap in "cde" else {}
        return [cap]
    loop = L.loop(rt, Driver(rt, steps), verifier=never)
    lid = loop.start(LoopSpec("g", limits=HardLimits(max_iterations=5), soft=SoftThresholds(repeat_failures=99, stall_iterations=2, plan_repeats=99)))
    st = loop.run(lid)
    assert st.status == "limit" and not any(e.type == "Diagnosed" for e in loop._raw.for_mission(lid))


def test_the_same_plan_again_with_no_progress_is_plan_cycling(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    d = Driver(rt, lambda c: ["a"])
    loop = L.loop(rt, d, verifier=never)
    lid = loop.start(LoopSpec("g", soft=SoftThresholds(repeat_failures=99, stall_iterations=99, plan_repeats=2, max_replans=0)))
    st = loop.run(lid)
    # iteration 1 completes 'a' (progress); iterations 2 and 3 only repeat the same plan without anything new
    assert st.status == "escalated" and st.escalation["kind"] == "plan_cycling" and len(st.iterations) == 3


# ───────────────────────── never plan around a decision that belongs to a person ─────────────────────────
def test_a_mission_ceiling_escalates_instead_of_replanning(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    L.provider.delay["a"] = 0.06
    d = Driver(rt, lambda c: ["a", "b"], alternatives=["something cheaper"], budget=Budget(usd=100.0, wall_clock_s=0.03))
    d.sep = ">"
    loop = L.loop(rt, d, verifier=verified)
    lid = loop.start(LoopSpec("g"))
    st = loop.run(lid)
    assert st.status == "escalated" and st.escalation["kind"] == "hard_limit" and d.replans == [] and len(st.iterations) == 1
    assert "b" not in L.provider.effects


def test_an_authority_denial_escalates_and_is_never_planned_around(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch, profile="production", guard=True, fenced=True)
    rt = L.runtime()
    SqliteEnvelopeStore(L.db).put(AuthorityEnvelope(
        tenant="acme", principal="o", mission_id="env", issued_at=NOW - 10, expires_at=NOW + 100_000,
        grants=(Grant("nothing.relevant"),), budget=AutonomyBudget(usd=1000, max_actions=1000)))
    d = Driver(rt, lambda c: ["a", "b"], alternatives=["try c instead"], tenant="acme", principal="o")   # no standing-grant policy: a plain scope check
    loop = L.loop(rt, d, verifier=verified)
    lid = loop.start(LoopSpec("g", tenant="acme"))
    st = loop.run(lid)
    assert st.status == "escalated" and st.escalation["kind"] == "authority_denied"
    assert d.replans == [] and len(st.iterations) == 1                      # no replan, no second attempt around the denial
    assert L.provider.effects == {}


def test_an_envelope_that_needs_extending_parks_for_a_person_and_the_loop_waits(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch, profile="production", guard=True, fenced=True)
    rt = L.runtime()
    SqliteEnvelopeStore(L.db).put(AuthorityEnvelope(
        tenant="acme", principal="o", mission_id="env", issued_at=NOW - 10, expires_at=NOW + 100_000,
        grants=(Grant("op.a"),), budget=AutonomyBudget(usd=1000, max_actions=1000)))
    d = Driver(rt, lambda c: ["a", "b"], alternatives=["try c instead"], **L.mission_kw())
    loop = L.loop(rt, d, verifier=verified)
    lid = loop.start(LoopSpec("g", tenant="acme"))
    st = loop.run(lid)
    assert st.status == "waiting_human" and d.replans == [] and len(st.iterations) == 1
    assert classify_loop(st).needs_a_person


def test_a_failed_compensation_escalates(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    d = Driver(rt, lambda c: ["a"], alternatives=["again"])
    loop = L.loop(rt, d, verifier=verified)
    lid = loop.start(LoopSpec("g"))
    real = rt.run

    def compensation_failed(mid):
        m = real(mid)
        m.state, m.outcome = MissionState.COMPENSATION_FAILED, {"compensation_incomplete": True}
        return m
    rt.run = compensation_failed
    st = loop.run(lid)
    assert st.status == "escalated" and st.escalation["kind"] == "needs_attention" and d.replans == []


# ───────────────────────── uncertainty is never planned around ─────────────────────────
def guarded_lab(tmp_path, monkeypatch):
    return lab(tmp_path, monkeypatch, profile="production", guard=True, fenced=True)


def test_an_unknown_outcome_holds_the_loop_and_the_same_mission_finishes_after_reconciliation(tmp_path, monkeypatch):
    L = guarded_lab(tmp_path, monkeypatch)
    L.provider.mode["b"] = "timeout"                                        # b is sent, applies, and the caller sees a timeout
    rt = L.runtime()
    d = Driver(rt, lambda c: ["a", "b", "c"], alternatives=["redo b a different way"], **L.mission_kw())
    loop = L.loop(rt, d, verifier=verified)
    lid = loop.start(LoopSpec("g", tenant="acme", limits=HardLimits(max_iterations=5)))
    st = loop.run(lid)
    assert st.status == "waiting_reconciliation" and len(st.iterations) == 1
    mid = st.iterations[0].mission_id
    assert rt._missions[mid].state == MissionState.WAITING_RECONCILIATION
    for _ in range(5):                                                      # however often it is driven or ticked: nothing starts, nothing is sent
        loop.run(lid)
        loop.tick()
    assert len(loop.state(lid).iterations) == 1 and L.provider.effects["b"] == 1 and d.replans == []
    assert L.provider.calls.count("b") == 1
    # evidence arrives: the SAME mission resumes (it does not resend b), and the loop finishes on it
    nb = rt.repo.pending_reconciliation(mid)[0]["node_id"]
    with L.leases.acquire(mid, owner="ops-console"):                        # a managed mission is only written by its lease holder
        rt.reconcile_node(mid, nb, executed=True, evidence=EVIDENCE, actor="ops@acme", reason="charge is in the dashboard", result={"charge": "ch_b"})
    st = loop.run(lid)
    assert st.status == "completed" and len(st.iterations) == 1
    assert L.provider.effects == {"a": 1, "b": 1, "c": 1} and L.provider.calls.count("b") == 1


def test_an_iteration_that_failed_around_an_unknown_effect_does_not_start_or_replan_anything(tmp_path, monkeypatch):
    """The mission FAILED (c was rejected) while b - a sibling in the same wave - is unresolved. A new iteration or a replan could send b
    again, so the loop holds until b is reconciled, then carries on."""
    L = guarded_lab(tmp_path, monkeypatch)
    L.provider.mode.update(b="timeout", c="fail")
    rt = L.runtime()
    d = Driver(rt, lambda c: ["b", "c"], alternatives=["avoid b"], **L.mission_kw())
    loop = L.loop(rt, d, verifier=verified)
    lid = loop.start(LoopSpec("g", tenant="acme", soft=SoftThresholds(repeat_failures=1, stall_iterations=1, plan_repeats=99)))
    st = loop.run(lid)
    mid = st.iterations[0].mission_id
    assert rt.repo.pending_reconciliation(mid) and rt._missions[mid].state in (MissionState.FAILED, MissionState.WAITING_RECONCILIATION)
    assert st.status == "waiting_reconciliation" and len(st.iterations) == 1 and d.replans == []
    for _ in range(3):
        loop.run(lid)
        loop.tick()
    assert len(loop.state(lid).iterations) == 1 and d.replans == [] and L.provider.calls.count("b") == 1


def test_a_waiting_loop_is_not_a_stall_and_is_not_watchdog_eligible(tmp_path, monkeypatch):
    L = guarded_lab(tmp_path, monkeypatch)
    L.provider.mode["b"] = "timeout"
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["a", "b"], **L.mission_kw()), verifier=verified)
    lid = loop.start(LoopSpec("g", tenant="acme"))
    st = loop.run(lid)
    a = classify_loop(st, now=time.time() + 10 ** 6)                        # however long it has been quiet
    assert a.kind is Wait.WAITING_OBSERVATION and a.watchdog_eligible is False and not any(e.type == "Diagnosed" for e in loop._raw.for_mission(lid))


def test_the_tick_continues_a_loop_whose_mission_a_probe_resolved(tmp_path, monkeypatch):
    from agentic_os.mission.probes import ProbeOutcome as O, ProbeRegistry, ProbeResult, Qualification
    from agentic_os.mission.reconcile_jobs import ReconcileJobs, ReconciliationSupervisor, RetryPolicy
    L = guarded_lab(tmp_path, monkeypatch)
    L.provider.mode["b"] = "timeout"
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["a", "b"], **L.mission_kw()), verifier=verified)
    lid = loop.start(LoopSpec("g", tenant="acme"))
    assert loop.run(lid).status == "waiting_reconciliation"
    reg = ProbeRegistry()
    reg.register("op.*", lambda ctx: ProbeResult(O.CONFIRMED_EXECUTED, evidence=dict(EVIDENCE), provider_ref="ch_b", result={"charge": "ch_b"}, observed_at=ctx.now),
                 Qualification("provider", eventually_consistent=False, authoritative_negative=True))
    now = [time.time()]
    sup = ReconciliationSupervisor(rt, ReconcileJobs(str(tmp_path / "jobs.db")), reg, policy=RetryPolicy(max_probes=3, initial_s=1, max_s=5, factor=2.0, jitter=0.0),
                                   clock=lambda: now[0], mission_leases=L.leases)
    sup.scan()
    now[0] += 60
    sup.tick()
    assert loop.tick()[lid] == "completed"
    assert L.provider.effects["b"] == 1 and L.provider.calls.count("b") == 1


# ───────────────────────── checkpoint / resume ─────────────────────────
class Crash(BaseException):
    pass


def test_a_crash_between_a_finished_mission_and_its_record_resumes_the_same_iteration(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    boom = {"on": True}

    def verify(ctx, m):
        if boom["on"]:
            boom["on"] = False
            raise Crash()
        return verified(ctx, m)
    loop = L.loop(rt, Driver(rt, lambda c: ["a", "b"]), verifier=verify)
    lid = loop.start(LoopSpec("g"))
    with pytest.raises(Crash):
        loop.run(lid)
    assert L.provider.effects == {"a": 1, "b": 1} and loop.state(lid).open_iteration is not None
    rt2 = L.runtime()                                                       # a new process over the same durable files
    d2 = Driver(rt2, lambda c: ["a", "b"])
    st = L.loop(rt2, d2, verifier=verified).run(lid)
    assert st.status == "completed" and len(st.iterations) == 1 and d2.started == []
    assert L.provider.effects == {"a": 1, "b": 1}                           # the finished work was not rerun


def test_a_restart_keeps_counters_so_a_crash_cannot_buy_more_replans(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    L.provider.mode["c"] = "fail"
    rt = L.runtime()
    d = Driver(rt, lambda c: ["c"], alternatives=["x", "y", "z"])
    soft = SoftThresholds(repeat_failures=2, stall_iterations=99, plan_repeats=99, max_replans=1)
    loop = L.loop(rt, d, verifier=verified)
    lid = loop.start(LoopSpec("g", soft=soft, limits=HardLimits(max_iterations=20)))
    assert loop.run(lid).status == "escalated"
    rt2 = L.runtime()
    d2 = Driver(rt2, lambda c: ["c"], alternatives=["x", "y", "z"])
    st = L.loop(rt2, d2, verifier=verified).run(lid)
    assert st.status == "escalated" and st.replans == 1 and d2.replans == [] and d2.started == []


def test_the_fold_is_pure_and_equal_after_a_reload(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    L.provider.mode["c"] = "fail"
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["a", "c"], alternatives=["q"]), verifier=verified)
    lid = loop.start(LoopSpec("g", soft=SoftThresholds(repeat_failures=2, stall_iterations=99, plan_repeats=99)))
    loop.run(lid)
    live = loop.state(lid).to_dict()
    tup = [(e.type, e.payload, e.ts) for e in EventStore(L.loop_log).for_mission(lid)]
    assert fold_loop(tup, lid).to_dict() == live == fold_loop(tup, lid).to_dict()


# ───────────────────────── cancellation ─────────────────────────
def test_cancelling_a_waiting_loop_cancels_the_mission_and_lists_what_it_may_have_left(tmp_path, monkeypatch):
    L = guarded_lab(tmp_path, monkeypatch)
    L.provider.mode["b"] = "timeout"
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["a", "b", "c"], **L.mission_kw()), verifier=verified)
    lid = loop.start(LoopSpec("g", tenant="acme"))
    st = loop.run(lid)
    mid = st.iterations[0].mission_id
    st = loop.cancel(lid, actor="ops@acme", reason="customer withdrew the order")
    assert st.status == "cancelled" and rt._missions[mid].state == MissionState.CANCELLED
    assert [u["mission_id"] for u in st.closed["unresolved_reconciliations"]] == [mid]          # an uncertainty is reported, never hidden
    assert rt.repo.pending_reconciliation(mid) and L.provider.calls.count("b") == 1
    assert loop.cancel(lid, actor="ops@acme", reason="again, to be sure").status == "cancelled"        # idempotent
    assert "LOOP_CANCELLED" in L.kinds()
    ts = [e.type for e in loop._raw.for_mission(lid)]
    assert ts.index("LoopCancelRequested") < ts.index("LoopCancelled")                          # the intent is on the record first


@pytest.mark.parametrize("sep", [">", ","], ids=["next_wave", "same_wave"])
def test_cancelling_during_a_run_stops_further_waves(tmp_path, monkeypatch, sep):
    L = lab(tmp_path, monkeypatch)
    L.provider.gate["a"] = threading.Event()
    rt = L.runtime()
    d = Driver(rt, lambda c: ["a", "b"])
    d.sep = sep                                                             # '>' : b needs a (a LATER wave); ',' : the same wave
    loop = L.loop(rt, d, verifier=verified, leases=None)
    lid = loop.start(LoopSpec("g"))
    out = {}
    t = threading.Thread(target=lambda: out.update(st=loop.run(lid)), daemon=True)
    t.start()
    end = time.time() + 5
    while "a" not in L.provider.calls and time.time() < end:
        time.sleep(0.01)
    loop.cancel(lid, actor="ops@acme", reason="stop everything now")
    L.provider.gate["a"].set()                                              # the call already in flight completes: it cannot be killed
    t.join(10)
    assert out["st"].status == "cancelled"
    assert L.provider.effects == {"a": 1}                                   # but nothing was dispatched after the cancel


def test_cancel_needs_a_named_person_and_a_reason(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["a"]), verifier=never)
    lid = loop.start(LoopSpec("g"))
    with pytest.raises(LoopError):
        loop.cancel(lid, actor="", reason="a good reason here")
    with pytest.raises(LoopError):
        loop.cancel(lid, actor="ops", reason="no")


def test_a_mission_cancelled_from_outside_closes_the_loop(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    rt = L.runtime()
    d = Driver(rt, lambda c: ["a"])
    loop = L.loop(rt, d, verifier=verified)
    lid = loop.start(LoopSpec("g"))
    real = d.start

    def start_then_cancel(ctx):
        mid = real(ctx)
        rt.cancel_mission(mid, reason="cancelled by someone else")
        return mid
    d.start = start_then_cancel
    assert loop.run(lid).status == "cancelled" and L.provider.effects == {}


# ───────────────────────── ownership: one driver, enforced at the write ─────────────────────────
def test_a_second_controller_cannot_drive_a_loop_that_is_held(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch, fenced=True)
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["a"]), verifier=verified, owner="ctl-1")
    lid = loop.start(LoopSpec("g"))
    held = L.leases.acquire(lid, owner="ctl-2")
    n = len(loop._raw.for_mission(lid))
    with pytest.raises(LoopBusy):
        loop.run(lid)
    assert len(loop._raw.for_mission(lid)) == n and L.provider.effects == {}
    held.release()
    assert loop.run(lid).status == "completed"


def test_a_stale_controller_cannot_write_to_the_loop_log(tmp_path, monkeypatch):
    clock = [NOW]
    L = lab(tmp_path, monkeypatch, fenced=True, clock=lambda: clock[0])
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["a"]), verifier=verified, owner="ctl-1")
    lid = loop.start(LoopSpec("g"))
    stale = L.leases.acquire(lid, owner="ctl-1")                            # ctl-1 holds the lease ... and stalls
    clock[0] += 120                                                         # ... past its expiry
    new = L.leases.acquire(lid, owner="ctl-2")
    assert new is not None and new.token > stale.token
    n = len(loop._raw.for_mission(lid))
    L.leases.held[lid] = stale                                              # the stalled process still believes it owns the loop
    with pytest.raises(FencingError):
        loop._append(lid, "IterationStarted", {"n": 1, "mission_id": "m", "at": 0})
    assert len(loop._raw.for_mission(lid)) == n                             # nothing was written


def test_a_stale_mission_driver_cannot_append_to_the_mission(tmp_path, monkeypatch):
    clock = [NOW]
    L = lab(tmp_path, monkeypatch, fenced=True, clock=lambda: clock[0])
    rt = L.runtime()
    m = rt.create_mission("g|a", policy_refs=["*"])
    stale = L.leases.acquire(m.id, owner="ctl-1")
    clock[0] += 120
    assert L.leases.acquire(m.id, owner="ctl-2") is not None
    L.leases.held[m.id] = stale
    with pytest.raises(FencingError):
        rt.run(m.id)
    assert L.provider.effects == {}


def test_the_production_profile_requires_leases_and_a_fenced_mission_store(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch, profile="production", guard=True, fenced=True)
    rt = L.runtime()
    d = Driver(rt, lambda c: [])
    with pytest.raises(LoopError, match="leases"):
        GoalLoop(rt, d, store=EventStore(L.loop_log), leases=None)
    plain = MissionRuntime(registry(), Executor(InMemoryOperatorClient({})), store=EventStore(str(tmp_path / "x.jsonl")),
                           authority=AuthorityGuard(SqliteEnvelopeStore(L.db), SqliteLedger(L.db + ".usage")), authority_key=lambda m: "env")
    with pytest.raises(LoopError, match="FencedEventStore"):
        GoalLoop(plain, d, store=EventStore(L.loop_log), leases=L.leases)
    GoalLoop(rt, d, store=EventStore(L.loop_log), leases=L.leases)           # the right wiring is accepted


def test_the_missions_run_under_their_own_lease(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch, fenced=True)
    rt = L.runtime()
    held_during = {}
    real = rt.run

    def spy(mid):
        held_during["held"] = mid in L.leases.held
        return real(mid)
    rt.run = spy
    loop = L.loop(rt, Driver(rt, lambda c: ["a"]), verifier=verified)
    assert loop.run(loop.start(LoopSpec("g"))).status == "completed" and held_during["held"] is True


# ───────────────────────── classification ─────────────────────────
def test_only_a_quiet_running_loop_is_watchdog_eligible(tmp_path, monkeypatch):
    L = lab(tmp_path, monkeypatch)
    L.provider.mode["c"] = "fail"
    rt = L.runtime()
    loop = L.loop(rt, Driver(rt, lambda c: ["c"]), verifier=verified)
    lid = loop.start(LoopSpec("g", soft=SoftThresholds(repeat_failures=2, max_replans=0)))
    st = loop.run(lid)
    assert classify_loop(st).kind is Wait.WAITING_ESCALATED and classify_loop(st).needs_a_person and not classify_loop(st).watchdog_eligible
    fresh = fold_loop([("LoopStarted", {"loop_id": "l", "spec": {"goal": "g", "limits": {}, "soft": {}}}, 100.0)], "l")
    assert classify_loop(fresh, now=110.0).kind is Wait.ACTIVE
    quiet = classify_loop(fresh, now=100.0 + 5000)
    assert quiet.kind is Wait.HUNG and quiet.watchdog_eligible
    stalled = fold_loop([("LoopStarted", {"loop_id": "l", "spec": {"goal": "g", "limits": {}, "soft": {}}}, 100.0),
                         ("IterationStarted", {"n": 1, "mission_id": "m"}, 101.0),
                         ("IterationFinished", {"n": 1, "outcome": "failed", "failure_sig": "s", "plan_sig": "p"}, 102.0),
                         ("Diagnosed", {"kind": "no_progress"}, 103.0)], "l")
    a = classify_loop(stalled, now=104.0)
    assert a.kind is Wait.STALLED and not a.watchdog_eligible
    assert classify_loop(fold_loop([("LoopStarted", {"loop_id": "l", "spec": {"goal": "g", "limits": {}, "soft": {}}}, 1.0),
                                    ("LoopCompleted", {"verified": True}, 2.0)], "l")).kind is Wait.TERMINAL
