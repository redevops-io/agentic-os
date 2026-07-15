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
from .store import EventStore
from . import pilots


def build_pilot_runtime(store_path: str | None = None, planner: Planner | None = None) -> MissionRuntime:
    operators = pilots.build_pilot_operators()
    registry = pilots.build_pilot_registry(operators)
    client = LocalOperatorClient(operators)
    return MissionRuntime(registry, Executor(client), store=EventStore(path=store_path),
                          planner=planner or default_planner())


def mount(app, runtime: MissionRuntime | None = None) -> MissionRuntime:
    rt = runtime or build_pilot_runtime()
    app.include_router(build_router(rt))
    app.include_router(build_capabilities_router(rt))
    app.include_router(build_cockpit_router(rt))   # P6: /inbox + /cockpit
    return rt


def build_mission_app():
    from fastapi import FastAPI
    app = FastAPI(title="Mission Runtime")
    mount(app)
    return app
