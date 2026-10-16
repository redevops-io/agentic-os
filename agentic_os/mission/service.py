"""Wire Mission Runtime into a control plane (or run it standalone).

`build_pilot_runtime()` assembles a runtime over the pilot operators (in-process via
LocalOperatorClient) with the env-selected planner. `mount(app)` adds the `/missions` +
`/capabilities` routers to an existing FastAPI app; `build_mission_app()` returns a standalone one.
"""
from __future__ import annotations

from .api import build_router, build_capabilities_router, build_cockpit_router
from .executor import Executor
from .factory import default_planner
from .operator_sdk import LocalOperatorClient
from .planner import Planner
from .runtime import MissionRuntime
from .event_backends import build_event_store
from . import pilots


def build_pilot_runtime(store_path: str | None = None, planner: Planner | None = None) -> MissionRuntime:
    operators = pilots.build_pilot_operators()
    registry = pilots.build_pilot_registry(operators)
    client = LocalOperatorClient(operators)
    return MissionRuntime(registry, Executor(client), store=build_event_store(store_path),
                          planner=planner or default_planner())


class RehydrateFailed(RuntimeError):
    """Strict mode only: one or more in-flight missions in the log could not be restored."""


def rehydrate_in_flight(rt: MissionRuntime, strict: bool | None = None) -> list[str]:
    """Restore every non-terminal mission found in the event log; return the ids restored.

    Lenient by default: each mission that cannot be rebuilt (authority or policy the log does not record, a
    plan that no longer reproduces) is logged and quarantined (``rt.quarantined[id] = reason`` plus one
    ``MissionQuarantined`` event) and never granted authority it cannot prove; every other mission is restored.
    ``strict=True`` (or ``AGENTIC_OS_REHYDRATE_STRICT=1``) raises ``RehydrateFailed`` instead.
    """
    import logging
    import os
    from .types import MissionState
    log = logging.getLogger(__name__)
    if strict is None:
        strict = os.environ.get("AGENTIC_OS_REHYDRATE_STRICT", "0").strip().lower() in ("1", "true", "yes")
    terminal = {MissionState.SUCCEEDED, MissionState.FAILED, MissionState.CANCELLED,
                MissionState.COMPENSATION_FAILED}
    quarantined = rt.quarantined
    restored, failed = [], []
    for mid in list(rt.store.mission_ids()):
        if mid in rt._missions:
            continue
        if rt.repo.state(mid) in terminal:
            continue
        try:
            m = rt.rehydrate(mid)
            restored.append(mid)
            created = next((e for e in rt.store.for_mission(mid) if e.type == "MissionCreated"), None)
            if created is not None and "budget" not in created.payload:
                log.warning("mission %s: no recorded budget, restored with the default", mid)
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {e}"
            failed.append(f"{mid}: {reason}")
            quarantined[mid] = reason
            log.warning("mission %s quarantined on restart: %s", mid, reason)
            if not any(ev.type == "MissionQuarantined" for ev in rt.store.for_mission(mid)):
                rt.store.append("MissionQuarantined", mid, {"reason": reason[:300]})
    if failed and strict:
        raise RehydrateFailed("; ".join(failed))
    return restored


def mount(app, runtime: MissionRuntime | None = None) -> MissionRuntime:
    from .control_auth import validate_config
    validate_config()                      # fail at startup on duplicate / colliding tokens
    rt = runtime or build_pilot_runtime()
    rehydrate_in_flight(rt)                # in-flight missions survive a restart (approve / GET work again)
    app.include_router(build_router(rt))
    app.include_router(build_capabilities_router(rt))
    app.include_router(build_cockpit_router(rt))   # P6: /inbox + /cockpit
    return rt


def build_mission_app():
    from fastapi import FastAPI
    app = FastAPI(title="Mission Runtime")
    mount(app)
    return app
