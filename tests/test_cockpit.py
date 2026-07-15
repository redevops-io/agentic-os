"""P6 — the human-facing shell: mission list, inbox (approvals + disambiguations), EXPLAIN over
the world-state timeline, and the cockpit page. A non-engineer can run a mission end-to-end."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.service import build_mission_app
from agentic_os.mission.api import build_cockpit_router
from agentic_os.mission.types import CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep


def test_cockpit_runs_a_mission_end_to_end():
    c = TestClient(build_mission_app())

    assert c.get("/cockpit").status_code == 200                      # the shell renders
    assert "Mission Plane" in c.get("/cockpit").text
    assert c.get("/missions").json()["missions"] == []               # empty to start
    assert c.get("/inbox").json()["inbox"] == []

    created = c.post("/missions", json={"goal": "Onboard a new customer",
                                        "template": "onboarding", "policy_refs": ["*"]}).json()
    mid = created["id"]
    assert created["state"] == "waiting_human"

    # the mission shows in the list, flagged as awaiting a human
    listed = c.get("/missions").json()["missions"]
    assert listed[0]["id"] == mid and listed[0]["awaiting_human"] is True

    # the inbox surfaces the approval task
    inbox = c.get("/inbox").json()["inbox"]
    assert len(inbox) == 1 and inbox[0]["mission_id"] == mid and inbox[0]["kind"] == "approval"
    node_id = inbox[0]["node_id"]

    # EXPLAIN over the world-state timeline
    ex = c.get(f"/missions/{mid}/explain").json()
    assert ex["state"] == "waiting_human" and "subscription" in ex["world_state"] and "learning" in ex

    # approve from the cockpit -> mission completes, inbox clears
    done = c.post(f"/missions/{mid}/approve", json={"node_id": node_id, "decision": "approve"}).json()
    assert done["state"] == "succeeded"
    assert c.get("/inbox").json()["inbox"] == []
    ex2 = c.get(f"/missions/{mid}/explain").json()
    assert ex2["state"] == "succeeded" and set(ex2["world_state"]) >= {"subscription", "consent_filed"}


def test_inbox_aggregates_approval_and_disambiguation():
    # one runtime with two missions: an onboarding (approval gate) + a conflict (disambiguation gate)
    from agentic_os.mission import pilots
    operators = pilots.build_pilot_operators()
    reg = pilots.build_pilot_registry(operators)
    # add a conflict fleet's specs + handlers into the same runtime
    reg.register(CapabilityManifest("crm2", [CapabilitySpec("crm2.pull", "crm2", provides=["sig"])]))
    reg.register(CapabilityManifest("off2", [CapabilitySpec("off2.make", "off2", provides=["deal"])]))
    from agentic_os.mission.operator_sdk import LocalOperatorClient

    class _MultiClient:
        def __init__(self, local): self.local = local
        def invoke(self, operator, capability, inputs, key):
            if capability == "crm2.pull":
                return {"_observations": [{"key": "tier", "value": "gold", "source": "a"},
                                          {"key": "tier", "value": "silver", "source": "b"}]}
            if capability == "off2.make":
                return {"deal": inputs.get("tier")}
            return self.local.invoke(operator, capability, inputs, key)

    rt = MissionRuntime(reg, Executor(_MultiClient(LocalOperatorClient(operators))))
    rt.create_mission("Onboard a new customer", template="onboarding", policy_refs=["*"])

    class _P:
        def plan(self, mid, g, ctx):
            return ExecutionIntent(mission_id=mid, steps=[
                IntentStep(outcome="sig", need="pull"),
                IntentStep(outcome="deal", need="make deal", inputs_from=["sig", "tier"])])
    rt.planner = _P()
    m2 = rt.create_mission("close a deal", policy_refs=["*"])
    # run both
    for mid in rt.store.mission_ids():
        rt.run(mid)

    app = FastAPI(); app.include_router(build_cockpit_router(rt))
    inbox = TestClient(app).get("/inbox").json()["inbox"]
    kinds = {i["kind"] for i in inbox}
    assert kinds == {"approval", "disambiguation"} and len(inbox) == 2
