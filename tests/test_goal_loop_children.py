"""Child agents under a goal loop: the loop beats their liveness leases for exactly as long as it is DRIVING a mission (including children spawned while it runs), ends them when it completes or is
cancelled, stops beating when it only waits, never revives a revoked or expired child, and a keepalive that fails never takes the loop down."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_child_agents import Lab as ChildLab, parent_env, Clock               # noqa: E402
from test_goal_loop import Driver, Lab as LoopLab, Provider, NOW as LOOP_NOW, verified, never   # noqa: E402

from agentic_os.mission.authority import AuthorityEnvelope, AutonomyBudget, Grant, SqliteEnvelopeStore   # noqa: E402
from agentic_os.mission.children import ACTIVE, CLOSED, REVOKED, ChildRegistry, ChildSpawner            # noqa: E402
from agentic_os.mission.goal_loop import GoalLoop, HardLimits, LoopSpec                                  # noqa: E402
from agentic_os.mission.sentinel import SentinelFeed                                                     # noqa: E402
from agentic_os.mission.store import EventStore                                                          # noqa: E402

SECRET = "k" * 64
TTL = 0.5                                                                       # a short lease; the beats come every third of it, so a stall of a few hundred ms on a busy runner is tolerated


# ───────────────────────── the tree keepalive on its own (fake clock, deterministic) ─────────────────────────
@pytest.fixture
def lab(tmp_path, monkeypatch):
    return ChildLab(tmp_path, monkeypatch)


def grand(lab):
    c = lab.spawn(may_delegate=True, grants=(Grant("crm.read"),), budget=dict(max_children=1, max_depth=1), client="app")
    g = lab.spawn(parent=c.key, role="sub", usd=1, tokens=10, actions=2, client="app")
    return c, g


def test_one_beat_extends_every_active_descendant_not_only_direct_children(lab):
    c, g = grand(lab)
    lab.clock.t += 40
    ka = lab.spawner.keepalive_tree("root")
    assert sorted(ka.beat_once()) == sorted([c.child_id, g.child_id])
    lab.clock.t += 40                                                            # past the ORIGINAL lease, inside the extended one
    ver = lab.spawner.verifier()
    assert ver.verify(c.credential, tenant="acme", client="app").ok and ver.verify(g.credential, tenant="acme", client="app").ok


def test_a_finished_child_is_not_beaten_and_is_not_reported_as_lost(lab):
    a, b = lab.spawn(usd=1), lab.spawn(usd=1)
    lab.spawner.close(a.child_id)
    lost: list = []
    assert lab.spawner.keepalive_tree("root", on_lost=lambda *x: lost.append(x)).beat_once() == [b.child_id] and lost == []


def test_a_revoked_child_is_never_revived_and_a_lapsed_one_is(lab):
    a, b = lab.spawn(usd=1), lab.spawn(usd=1)
    lab.spawner.revoke(a.child_id, actor="ops@acme", reason="misbehaving")
    lab.clock.t += 500                                                           # the driver paused far beyond the lease
    ver = lab.spawner.verifier()
    assert not ver.verify(b.credential, tenant="acme").ok
    assert lab.spawner.keepalive_tree("root").beat_once() == [b.child_id]
    assert ver.verify(b.credential, tenant="acme").ok and lab.registry.get(a.child_id)["status"] == REVOKED


def test_a_child_whose_authority_ran_out_while_still_active_is_reported_once(lab):
    c = lab.spawn(ttl_s=100)
    lost: list = []
    ka = lab.spawner.keepalive_tree("root", on_lost=lambda cid, why: lost.append((cid, why)))
    lab.clock.t += 101                                                           # its envelope expired; the registry still says active
    assert ka.beat_once() == [] and ka.beat_once() == []
    assert lost == [(c.child_id, "expired")]


def test_entering_the_context_revives_a_lapsed_child_at_once_not_a_third_of_a_lease_later(lab):
    c = lab.spawn(client="app")
    lab.clock.t += 500                                                           # the loop was paused far longer than the lease
    ver = lab.spawner.verifier()
    assert not ver.verify(c.credential, tenant="acme", client="app").ok
    with lab.spawner.keepalive_tree("root"):
        assert ver.verify(c.credential, tenant="acme", client="app").ok          # before any background beat could have run


def test_a_child_that_ended_just_before_its_beat_is_not_reported_as_lost(lab):
    c = lab.spawn(usd=1)
    stale = lab.registry.descendants("root")                                      # the listing still says active ...
    lab.spawner.close(c.child_id)                                                 # ... but the child ended before the beat
    lab.registry.descendants = lambda key: stale
    lost: list = []
    assert lab.spawner.keepalive_tree("root", on_lost=lambda *x: lost.append(x)).beat_once() == [] and lost == []


def test_a_raising_observer_or_registry_does_not_stop_the_beats(lab):
    a, b = lab.spawn(usd=1, ttl_s=100), lab.spawn(usd=1)
    ka = lab.spawner.keepalive_tree("root", on_lost=lambda *x: 1 / 0)
    lab.clock.t += 101
    lab.guard.envelopes.revoke(b.child_id)
    assert ka.beat_once() == []                                                  # both refused, the observer raised, nothing propagated


def test_end_tree_closes_grandchildren_deepest_first_and_returns_the_allowance(lab):
    c, g = grand(lab)
    before = lab.used()
    out = lab.spawner.end_tree("root", "work over", actor="loop:L1")
    assert [r["child_id"] for r in out] == [g.child_id, c.child_id]
    assert lab.registry.get(c.child_id)["status"] == CLOSED and lab.registry.get(g.child_id)["status"] == CLOSED
    assert lab.spawner.end_tree("root") == []                                    # idempotent
    assert before and lab.used().get("usd", 0) < before["usd"]


def test_the_beats_are_a_thread_that_stops_with_the_context(tmp_path, monkeypatch):
    L = ChildLab(tmp_path, monkeypatch, clock=time.time, env=parent_env(issued_at=time.time() - 1, expires_at=time.time() + 3600))
    L.spawner.lease_ttl_s = TTL
    a = L.spawn(client="app")
    ver = L.spawner.verifier()
    with L.spawner.keepalive_tree("root"):
        late = L.spawn(usd=1, client="app")                                      # spawned while the work is being driven
        time.sleep(1.2)                                                          # more than two lease periods of work
        assert ver.verify(a.credential, tenant="acme", client="app").ok and ver.verify(late.credential, tenant="acme", client="app").ok
    time.sleep(0.7)
    assert not ver.verify(a.credential, tenant="acme", client="app").ok and not ver.verify(late.credential, tenant="acme", client="app").ok


# ───────────────────────── the goal loop ─────────────────────────
class Hooked(Provider):
    """A provider whose capability runs a hook first (to spawn a child, revoke one, or look at a credential in the middle of the run) and then takes `delay` seconds."""

    def __init__(self):
        super().__init__()
        self.hook = None
        self.during: list = []

    def handler(self, cap):
        inner = super().handler(cap)

        def fn(inputs):
            if self.hook:
                self.hook()
            return inner(inputs)
        return fn


class World:
    def __init__(self, tmp_path, monkeypatch, *, with_children=True, ttl=TTL):
        self.lab = LoopLab(tmp_path, monkeypatch, guard=True)
        self.lab.provider = Hooked()
        SqliteEnvelopeStore(self.lab.db).put(AuthorityEnvelope(
            tenant="acme", principal="o", mission_id="env", issued_at=LOOP_NOW - 10, expires_at=LOOP_NOW + 100_000, may_delegate=True, grants=(Grant("op.*"),),
            budget=AutonomyBudget(usd=1000, tokens=100_000, wall_clock_s=100_000, max_actions=1000, max_children=6, max_depth=2, max_parallel=4)))
        self.rt = self.lab.runtime()
        self.events: list = []
        self.spawner = ChildSpawner(self.rt.authority, ChildRegistry(str(tmp_path / "children.db")), SECRET, lease_ttl_s=ttl, on_event=self.events.append)
        self.with_children = with_children
        self.feed_events: list = []

    def loop(self, driver, *, verifier=None):
        return GoalLoop(self.rt, driver, store=EventStore(self.lab.loop_log), verifier=verifier, on_event=self._on, owner="ctl-1", leases=None,
                        children=self.spawner if self.with_children else None)

    def _on(self, ev):
        self.lab.events.append(ev)

    def spawn(self, *, ttl_s=3000, usd=2, **kw):
        return self.spawner.spawn("env", "acme", role="worker", goal="gather", grants=(Grant("op.a"),), ttl_s=ttl_s, client="app", may_delegate=bool(kw.get("kids", 0)),
                                  budget=AutonomyBudget(usd=usd, tokens=100, wall_clock_s=600, max_actions=5, max_children=kw.get("kids", 0), max_depth=kw.get("kids", 0), max_parallel=1))

    def alive(self, child) -> bool:
        return self.spawner.verifier().verify(child.credential, tenant="acme", client="app").ok

    def driver(self, steps="a", **kw):
        return Driver(self.rt, lambda ctx: list(steps), **self.lab.mission_kw())

    def kinds(self):
        return self.lab.kinds()


def mid_run_probe(world, child_getter, sleep=1.2):
    """Called from inside the mission's capability: let four lease periods pass, then record whether the child's credential still verifies."""
    def hook():
        time.sleep(sleep)
        world.lab.provider.during.append(world.alive(child_getter()))
    return hook


def test_a_child_stays_alive_for_as_long_as_the_loop_drives_its_mission_and_dies_without_the_wiring(tmp_path, monkeypatch):
    for wired, expect in ((True, True), (False, False)):
        sub = tmp_path / ("wired" if wired else "plain")
        sub.mkdir()
        w = World(sub, monkeypatch, with_children=wired)
        child = w.spawn()
        w.lab.provider.hook = mid_run_probe(w, lambda c=child: c)
        loop = w.loop(w.driver(), verifier=verified)
        lid = loop.start(LoopSpec(goal="g", tenant="acme", limits=HardLimits(max_iterations=2)))
        loop.run(lid)
        assert w.lab.provider.during == [expect], f"wired={wired}"


def test_a_child_spawned_while_the_mission_runs_is_covered_from_its_first_beat(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    made: list = []

    def hook():
        c = w.spawn()
        made.append(c)
        time.sleep(1.2)
        w.lab.provider.during.append(w.alive(c))
    w.lab.provider.hook = hook
    loop = w.loop(w.driver(), verifier=verified)
    lid = loop.start(LoopSpec(goal="g", tenant="acme", limits=HardLimits(max_iterations=2)))
    loop.run(lid)
    assert w.lab.provider.during == [True] and len(made) == 1


def test_a_completed_loop_ends_its_children_grandchildren_included_and_records_it(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    c = w.spawn(kids=1)
    g = w.spawner.spawn(c.key, "acme", role="sub", goal="g", grants=(Grant("op.a"),), budget=AutonomyBudget(usd=1, tokens=10, wall_clock_s=60, max_actions=2, max_children=0, max_depth=0, max_parallel=1), client="app")
    loop = w.loop(w.driver(), verifier=verified)
    lid = loop.start(LoopSpec(goal="g", tenant="acme", limits=HardLimits(max_iterations=2)))
    st = loop.run(lid)
    assert st.status == "completed"
    assert w.spawner.registry.get(c.child_id)["status"] == CLOSED and w.spawner.registry.get(g.child_id)["status"] == CLOSED
    ended = [e for e in loop._raw.for_mission(lid) if e.type == "LoopChildrenEnded"]
    assert len(ended) == 1 and set(ended[0].payload["children"]) == {c.child_id, g.child_id} and "LOOP_CHILDREN_ENDED" in w.kinds()
    assert not w.alive(c)


def test_a_completed_but_unverified_loop_also_ends_its_children(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    c = w.spawn()
    loop = w.loop(w.driver(), verifier=None)
    lid = loop.start(LoopSpec(goal="g", tenant="acme", limits=HardLimits(max_iterations=2)))
    assert loop.run(lid).status == "completed_unverified" and w.spawner.registry.get(c.child_id)["status"] == CLOSED


def test_a_cancelled_loop_ends_its_children(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    c = w.spawn()
    loop = w.loop(w.driver(), verifier=never)
    lid = loop.start(LoopSpec(goal="g", tenant="acme", limits=HardLimits(max_iterations=1)))
    assert loop.run(lid).status == "limit"                                       # one iteration, goal not verified: the loop stops at its ceiling and may still be resumed
    assert w.spawner.registry.get(c.child_id)["status"] == ACTIVE                # a paused loop does not end its children
    loop.cancel(lid, actor="ops@acme", reason="no longer needed")
    assert loop.state(lid).status == "cancelled" and w.spawner.registry.get(c.child_id)["status"] == CLOSED


def test_a_paused_loop_stops_beating_and_a_resumed_one_revives_the_same_token(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    c = w.spawn()
    loop = w.loop(w.driver(), verifier=never)
    lid = loop.start(LoopSpec(goal="g", tenant="acme", limits=HardLimits(max_iterations=1)))
    assert loop.run(lid).status == "limit"
    time.sleep(0.8)                                                              # the loop is waiting for a person: nobody beats
    assert not w.alive(c) and w.spawner.registry.get(c.child_id)["status"] == ACTIVE
    w.lab.provider.hook = mid_run_probe(w, lambda: c, sleep=0.2)
    loop.extend_limits(lid, actor="ops@acme", reason="one more try", max_iterations=3)
    loop.run(lid)
    assert w.lab.provider.during and w.lab.provider.during[0] is True            # the SAME token works again once the loop drives a mission


def test_a_child_that_lost_its_authority_is_recorded_without_stopping_the_loop(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    short = w.spawn(ttl_s=0.3)                                                   # its envelope expires during the run
    w.lab.provider.hook = lambda: time.sleep(1.0)
    loop = w.loop(w.driver(), verifier=verified)
    lid = loop.start(LoopSpec(goal="g", tenant="acme", limits=HardLimits(max_iterations=2)))
    st = loop.run(lid)
    lost = [e for e in loop._raw.for_mission(lid) if e.type == "LoopChildLost"]
    assert st.status == "completed" and len(lost) == 1 and lost[0].payload["child_id"] == short.child_id and lost[0].payload["why"] == "expired"
    assert "LOOP_CHILD_LOST" in w.kinds()


def test_a_revoked_child_is_not_a_lost_child_and_is_never_revived(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    c = w.spawn()

    def hook():
        w.spawner.revoke(c.child_id, actor="ops@acme", reason="misbehaving worker")
        time.sleep(0.8)
    w.lab.provider.hook = hook
    loop = w.loop(w.driver(), verifier=verified)
    lid = loop.start(LoopSpec(goal="g", tenant="acme", limits=HardLimits(max_iterations=2)))
    loop.run(lid)
    assert not [e for e in loop._raw.for_mission(lid) if e.type == "LoopChildLost"]
    assert w.spawner.registry.get(c.child_id)["status"] == REVOKED and not w.alive(c)


def test_a_keepalive_that_fails_never_takes_the_loop_down(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.spawn()
    w.spawner.heartbeat = lambda cid: (_ for _ in ()).throw(RuntimeError("registry unavailable"))
    loop = w.loop(w.driver(), verifier=verified)
    lid = loop.start(LoopSpec(goal="g", tenant="acme", limits=HardLimits(max_iterations=2)))
    assert loop.run(lid).status == "completed"


def test_ending_children_never_stops_the_loop_reaching_its_terminal_state_and_the_failure_is_recorded(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.spawn()
    w.spawner.end_tree = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    loop = w.loop(w.driver(), verifier=verified)
    lid = loop.start(LoopSpec(goal="g", tenant="acme", limits=HardLimits(max_iterations=2)))
    assert loop.run(lid).status == "completed"
    ended = [e for e in loop._raw.for_mission(lid) if e.type == "LoopChildrenEnded"]
    assert ended and ended[0].payload["errors"] and ended[0].payload["children"] == []


def test_a_loop_without_a_spawner_behaves_exactly_as_before(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, with_children=False)
    c = w.spawn()
    loop = w.loop(w.driver(), verifier=verified)
    lid = loop.start(LoopSpec(goal="g", tenant="acme", limits=HardLimits(max_iterations=2)))
    assert loop.run(lid).status == "completed"
    assert w.spawner.registry.get(c.child_id)["status"] == ACTIVE and "LoopChildrenEnded" not in [e.type for e in loop._raw.for_mission(lid)]


def test_the_sentinel_feed_sees_a_lost_child_and_the_ending_of_children(tmp_path, monkeypatch):
    from agentic_os.mission.store import EventStore as ES
    feed = SentinelFeed(ES(str(tmp_path / "feed.jsonl")))
    w = World(tmp_path, monkeypatch)
    w.spawn(ttl_s=0.3)
    w.lab.provider.hook = lambda: time.sleep(1.0)
    loop = GoalLoop(w.rt, w.driver(), store=EventStore(w.lab.loop_log), verifier=verified, on_event=feed.loop_sink, owner="c", leases=None, children=w.spawner)
    lid = loop.start(LoopSpec(goal="g", tenant="acme", limits=HardLimits(max_iterations=2)))
    loop.run(lid)
    evs, _ = feed.pull(0)
    kinds = {e["kind"] for e in evs}
    assert "LOOP_CHILD_LOST" in kinds and "LOOP_CHILDREN_ENDED" in kinds
