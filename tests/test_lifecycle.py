"""Mission lifecycle contributors — data-only hooks, injected capabilities, never own loop control."""
from __future__ import annotations

from agentic_os.mission.lifecycle import (
    LifecycleContributor, LifecycleRegistry, MissionFinished, MissionStarted, SessionIdle,
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
