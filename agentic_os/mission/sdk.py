"""Mission SDK (v6 Phase 6.2) — describe missions and capabilities as code, not agent loops.

Two authoring surfaces, both thin over what the kernel already understands:

  * `@template(name)` registers a mission template — a decorated `fn(mission_id) -> [step, …]`
    becomes a `TEMPLATES` entry the planner specializes, so authoring a cross-app mission is a
    handful of declarative steps rather than a hand-built ExecutionIntent.
  * `step(outcome, need, after=…)` is an ergonomic `IntentStep` (the DAG edge is just `after`).

`Operator` + `capability` (the operator-side authoring surface) are re-exported so an app can
declare its capabilities and a mission from one import.
"""
from __future__ import annotations

from typing import Callable

from .operator_sdk import Operator, capability  # noqa: F401 — re-exported authoring surface
from .templates import TEMPLATES
from .types import ExecutionIntent, IntentStep

StepList = Callable[[str], list[IntentStep]]


def step(outcome: str, need: str, *, after: list[str] | None = None,
         value: str = "medium", constraints: list[str] | None = None) -> IntentStep:
    """One mission step: produce `outcome` by satisfying `need`, after the `after` outcomes.
    `need` is what capability discovery matches on; `outcome` is what a capability must `provide`."""
    return IntentStep(outcome=outcome, need=need, inputs_from=list(after or []),
                      value_hint=value, constraints=list(constraints or []))


def template(name: str) -> Callable[[StepList], StepList]:
    """Register a mission template. The decorated `fn(mission_id) -> [step, …]` becomes the
    `TEMPLATES[name]` factory the planner uses (via `templates.get(name, mission_id)`)."""
    def deco(build_steps: StepList) -> StepList:
        def _factory(mission_id: str, _build: StepList = build_steps) -> ExecutionIntent:
            return ExecutionIntent(mission_id=mission_id, rationale=f"{name} template",
                                   steps=list(_build(mission_id)))
        TEMPLATES[name] = _factory
        build_steps._template_name = name  # type: ignore[attr-defined]
        return build_steps
    return deco
