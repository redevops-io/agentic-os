"""Idempotent calls, shared event logs and mission runs stay correct under concurrency."""
import json
import threading
import time

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.operator_sdk import Operator, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import (CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep,
                                      MissionState)


def test_same_key_runs_handler_once():
    count = []

    def h(inp):
        time.sleep(0.1)
        count.append(1)
        return {"done": True, "n": len(count)}

    op = Operator("svc", [capability("act", h, side_effecting=True, outputs={"done": "bool"}, irreversible="test")])
    out = []
    ts = [threading.Thread(target=lambda: out.append(op.invoke("act", {"a": 1}, "k1"))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(count) == 1
    assert all(r == out[0] for r in out)
    assert op.invoke("act", {"a": 1}, "k1") == out[0] and len(count) == 1
    op.invoke("act", {"a": 2}, "k2")
    assert len(count) == 2


def test_failed_first_call_does_not_pin_the_key():
    n = []

    def h(inp):
        n.append(1)
        if len(n) == 1:
            raise RuntimeError("transient")
        return {"done": True}

    op = Operator("svc", [capability("act", h, outputs={"done": "bool"})])
    try:
        op.invoke("act", {}, "k")
    except RuntimeError:
        pass
    assert op.invoke("act", {}, "k") == {"done": True}


def test_call_log_is_bounded():
    op = Operator("svc", [capability("r", lambda i: {"ok": 1})])
    for _ in range(10_050):
        op.invoke("r", {}, "")
    assert len(op.calls) == 10_000


def test_two_writers_share_one_sequence(tmp_path):
    p = str(tmp_path / "e.jsonl")
    a, b = EventStore(p), EventStore(p)

    def write(store, mid):
        for i in range(25):
            store.append("X", mid, {"i": i})

    ts = [threading.Thread(target=write, args=(s, m)) for s, m in ((a, "m1"), (b, "m2"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    seqs = [json.loads(line)["seq"] for line in open(p)]
    assert seqs == list(range(1, 51))
    assert a.refresh() >= 0 and len(a.all()) == 50
    b.refresh()
    assert len(b.all()) == 50
    assert len(EventStore(p).all()) == 50


def test_torn_tail_is_ignored_and_trimmed(tmp_path):
    p = str(tmp_path / "e.jsonl")
    s = EventStore(p)
    s.append("X", "m", {})
    with open(p, "a") as f:
        f.write('{"seq": 2, "ts": 1, "type": "Y", "miss')
    assert len(EventStore(p).all()) == 1
    s2 = EventStore(p)
    ev = s2.append("Z", "m", {})
    assert ev.seq == 2
    assert [json.loads(line)["type"] for line in open(p)] == ["X", "Z"]


def _rt(handler):
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec("a", "op", provides=["oa"], side_effecting=True,
                                                          irreversible=True, irreversible_note="t",
                                                          permissions=["*"])]))

    class Planner:
        def plan(self, mission_id, goal, context):
            return ExecutionIntent(mission_id=mission_id, steps=[IntentStep(outcome="oa", need="do a")])

    return MissionRuntime(reg, Executor(InMemoryOperatorClient({"a": handler})), planner=Planner())


def test_concurrent_run_dispatches_once():
    calls = []

    def slow(i):
        calls.append(1)
        time.sleep(0.1)
        return {"ok": 1}

    rt = _rt(slow)
    m = rt.create_mission("g", policy_refs=["*"])
    ts = [threading.Thread(target=lambda: rt.run(m.id)) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1
    assert m.state == MissionState.SUCCEEDED


def test_cancelled_mission_is_not_run():
    calls = []
    rt = _rt(lambda i: calls.append(1) or {"ok": 1})
    m = rt.create_mission("g", policy_refs=["*"])
    rt.cancel_mission(m.id, "stop")
    rt.run(m.id)
    assert m.state == MissionState.CANCELLED and calls == []


def test_malformed_observation_is_ignored():
    rt = _rt(lambda i: {"ok": 1, "_observations": [{"value": 1}, "x", {"key": "k", "confidence": "bad"}]})
    m = rt.create_mission("g", policy_refs=["*"])
    rt.run(m.id)
    assert m.state == MissionState.SUCCEEDED
