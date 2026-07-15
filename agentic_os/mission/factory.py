"""Assemble a MissionRuntime from configuration — pick the planner and executor by env.

Keeps the wiring in one place so callers (control_plane, demo, tests) don't repeat it:

  * planner  — ModelPlanner(Qwen3-Coder-Next-NVFP4) if MISSION_PLANNER_BASE_URL is set, else the
    offline TemplatePlanner (so it always runs).
  * executor — an explicit `operator_client`, else an HTTPOperatorClient from MISSION_OPERATOR_BASES
    (a JSON map operator->base_url), else in-memory (caller supplies handlers) is a test concern.
"""
from __future__ import annotations

import json
import os

from .executor import Executor
from .models import OpenAICompatModel
from .operators import HTTPOperatorClient
from .planner import TemplatePlanner, ModelPlanner, Planner
from .registry import CapabilityRegistry
from .runtime import MissionRuntime
from .store import EventStore


def default_planner(transport=None) -> Planner:
    model = OpenAICompatModel.from_env(transport=transport)
    return ModelPlanner(model) if model is not None else TemplatePlanner()


def default_operator_client():
    bases = os.environ.get("MISSION_OPERATOR_BASES")
    if bases:
        return HTTPOperatorClient(json.loads(bases))
    return None


def build_runtime(registry: CapabilityRegistry, *, operator_client=None, store_path: str | None = None,
                  planner: Planner | None = None) -> MissionRuntime:
    client = operator_client or default_operator_client()
    if client is None:
        raise ValueError("no operator client: pass operator_client=, or set MISSION_OPERATOR_BASES")
    store = EventStore(path=store_path or os.environ.get("MISSION_EVENT_LOG"))
    return MissionRuntime(
        registry, Executor(client), store=store,
        planner=planner or default_planner(),
    )
