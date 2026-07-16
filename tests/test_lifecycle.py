"""Mission lifecycle contributors — data-only hooks, injected capabilities, never own loop control."""
from __future__ import annotations

from agentic_os.mission.lifecycle import (
    GateReached, LifecycleContributor, LifecycleRegistry, MissionFinished, MissionStarted, SessionIdle,
)


def test_dispatch_delivers_data_and_injected_capabilities():
    seen = []

    class Rec(LifecycleContributor):
        def on_mission_started(self, event, capabilities):
            seen.append(("start", event.mission_id, event.goal, capabilities.get("svc")))

    reg = LifecycleRegistry(capabilities={"svc": "evidence-log"}).install(Rec())
    reg.dispatch(MissionStarted(mission_id="m1", goal="deploy", template="deploy_app"))
    assert seen == [("start", "m1", "deploy", "evidence-log")]


def test_a_raising_contributor_never_breaks_the_loop():
    class Boom(LifecycleContributor):
        def on_mission_finished(self, event, capabilities):
            raise RuntimeError("contributor blew up")

    reg = LifecycleRegistry().install(Boom())
    # must NOT raise — an extension can never break orchestration
    reg.dispatch(MissionFinished(mission_id="m1", state="succeeded"))


def test_empty_registry_is_a_noop():
    LifecycleRegistry().dispatch(SessionIdle())  # no contributors → no-op, no error


def test_inject_adds_a_capability():
    got = []

    class C(LifecycleContributor):
        def on_session_idle(self, event, capabilities):
            got.append(capabilities.get("monitor"))

    reg = LifecycleRegistry().install(C()).inject("monitor", object())
    reg.dispatch(SessionIdle())
    assert got and got[0] is not None


# ── runtime wiring: the MissionRuntime dispatches lifecycle events ──
def test_runtime_dispatches_start_gate_and_finish():
    from agentic_os.mission.executor import Executor, InMemoryOperatorClient
    from agentic_os.mission.registry import CapabilityRegistry
    from agentic_os.mission.runtime import MissionRuntime
    from agentic_os.mission.types import (
        CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep,
    )

    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec(
        "op.act", "op", provides=["done"], side_effecting=True, confidence=0.3)]))  # dynamic-risk gate
    client = InMemoryOperatorClient({"op.act": lambda i: {"ok": True}})

    class _P:
        def plan(self, mid, goal, ctx):
            return ExecutionIntent(mission_id=mid, steps=[IntentStep(outcome="done", need="do")])

    rt = MissionRuntime(reg, Executor(client), planner=_P())
    events = []

    class Rec(LifecycleContributor):
        def on_mission_started(self, e, caps): events.append(("start", e.mission_id))
        def on_gate_reached(self, e, caps): events.append(("gate", e.capability))
        def on_mission_finished(self, e, caps): events.append(("finish", e.state))

    rt.lifecycle.install(Rec())
    m = rt.create_mission("act", policy_refs=["*"])
    rt.run(m.id)
    kinds = [k for k, _ in events]
    assert "start" in kinds and "gate" in kinds                     # gated mission → start + gate fired
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    assert "finish" in [k for k, _ in events]                       # terminal → MissionFinished fired


# ── the monitoring agent's AlertContributor turns a gate into a stakeholder alert ──
def test_alert_contributor_alerts_on_gate_and_finish():
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deploy", "sidekick-devops"))
    from notify import AlertContributor, Notifier

    sent = []

    class Stub(Notifier):
        def __init__(self): self.url = "x"
        def send(self, text, **f): sent.append((f.get("kind"), text)); return True

    reg = LifecycleRegistry().install(AlertContributor(notifier=Stub(), cockpit_url="http://cp"))
    reg.dispatch(GateReached(mission_id="m1", node_id="n1", capability="infra.provision"))
    reg.dispatch(MissionFinished(mission_id="m1", state="succeeded"))
    kinds = [k for k, _ in sent]
    assert "approval" in kinds and "finished" in kinds
    assert any("infra.provision" in t for _, t in sent)             # the gate names the held capability
