"""v6 Phase 6.2 — the Mission SDK: author a mission template as a few declarative steps."""
from __future__ import annotations

from agentic_os.mission import templates
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.sdk import Operator, capability, step, template
from agentic_os.mission.types import MissionState


def test_template_decorator_registers_a_usable_template():
    @template("welcome_flow")
    def _welcome(_mid):
        return [
            step("greeting_ready", "draft a welcome greeting for the customer"),
            step("greeting_sent", "send the welcome greeting", after=["greeting_ready"]),
        ]

    assert "welcome_flow" in templates.TEMPLATES
    intent = templates.get("welcome_flow", "m1")
    assert [s.outcome for s in intent.steps] == ["greeting_ready", "greeting_sent"]
    assert intent.steps[1].inputs_from == ["greeting_ready"]  # `after` becomes the DAG edge


def test_sdk_authored_mission_runs_end_to_end():
    @template("welcome_flow2")
    def _welcome(_mid):
        return [
            step("greeting_ready", "draft a welcome greeting"),
            step("greeting_sent", "send the welcome greeting", after=["greeting_ready"]),
        ]

    # an operator authored with the same SDK surface, providing the two outcomes
    op = Operator("concierge", [
        capability("concierge.draft", lambda i: {"text": "hi"}, provides=["greeting_ready"],
                   permissions=["concierge:write"]),
        capability("concierge.send", lambda i: {"sent": True}, provides=["greeting_sent"],
                   side_effecting=True, permissions=["concierge:write"]),
    ])
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    rt = MissionRuntime(reg, Executor(LocalOperatorClient({op.name: op})))

    m = rt.create_mission("Welcome the customer", policy_refs=["concierge:write"], template="welcome_flow2")
    rt.run(m.id)
    assert rt._missions[m.id].state == MissionState.SUCCEEDED
    assert {"greeting_ready", "greeting_sent"} <= set(rt._world(m.id).snapshot())
