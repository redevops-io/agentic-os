"""P2 wiring: real model planner + HTTP/Dagster executor seams — proven with fakes."""
from __future__ import annotations

import pytest

from agentic_os.mission.dagster_exec import DagsterOperatorClient, DagsterError
from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.factory import default_planner
from agentic_os.mission.models import strip_thinking, OpenAICompatModel
from agentic_os.mission.operators import HTTPOperatorClient
from agentic_os.mission.planner import ModelPlanner, TemplatePlanner
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import MissionState

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]

_MODEL_JSON = """<think>Decompose onboarding into outcomes and their deps.</think>
{"steps":[
 {"outcome":"subscription","need":"create a paid subscription","inputs_from":[]},
 {"outcome":"onboarding_sent","need":"send the welcome message","inputs_from":["subscription"]},
 {"outcome":"revenue_recorded","need":"record the revenue","inputs_from":["subscription"]},
 {"outcome":"consent_filed","need":"file the consent record","inputs_from":["revenue_recorded"]}
]}"""


class _FakeModel:
    def __init__(self, text): self.text = text
    def complete(self, prompt): return self.text


def test_strip_thinking():
    assert strip_thinking("<think>reason</think>{\"a\":1}") == '{"a":1}'


def test_model_planner_parses_and_runs_end_to_end():
    reg, client = build_fleet()
    rt = MissionRuntime(reg, Executor(client), planner=ModelPlanner(_FakeModel(_MODEL_JSON)))
    m = rt.create_mission("Onboard a customer", policy_refs=GRANTS)   # no template → planner drives
    assert len(rt._plans[m.id].graph.nodes) == 4
    rt.run(m.id)
    assert m.state == MissionState.WAITING_HUMAN
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    assert rt._missions[m.id].state == MissionState.SUCCEEDED


def test_model_planner_falls_back_on_bad_json():
    planner = ModelPlanner(_FakeModel("sorry, I cannot"))
    intent = planner.plan("mission_x", "onboard a new customer", {})   # falls back to template
    assert [s.outcome for s in intent.steps][0] == "subscription"


def test_http_operator_client_posts_invoke_with_idempotency():
    captured = {}
    def transport(url, body, headers, timeout):
        captured.update(url=url, body=body, headers=headers)
        return {"result": {"ok": True, "cap": body["capability"]}}
    c = HTTPOperatorClient({"agentic-support": "http://support:8207"}, transport=transport)
    r = c.invoke("agentic-support", "support.send_onboarding", {"_mission": "m1"}, "idem1")
    assert r == {"ok": True, "cap": "support.send_onboarding"}
    assert captured["url"] == "http://support:8207/invoke"
    assert captured["headers"]["Idempotency-Key"] == "idem1"


def test_dagster_client_launch_poll_returns_result():
    def gql(query, variables):
        if "launchRun" in query:
            return {"data": {"launchRun": {"__typename": "LaunchRunSuccess",
                                           "run": {"runId": "r1", "status": "STARTED"}}}}
        return {"data": {"runOrError": {"__typename": "Run", "runId": "r1", "status": "SUCCESS"}}}
    c = DagsterOperatorClient(gql=gql, poll_interval=0,
                              result_reader=lambda rid: {"done": True, "rid": rid})
    assert c.invoke("op", "cap", {"x": 1}, "k") == {"done": True, "rid": "r1"}


def test_dagster_client_raises_on_failed_run():
    def gql(query, variables):
        if "launchRun" in query:
            return {"data": {"launchRun": {"__typename": "LaunchRunSuccess", "run": {"runId": "r1"}}}}
        return {"data": {"runOrError": {"__typename": "Run", "status": "FAILURE"}}}
    with pytest.raises(DagsterError):
        DagsterOperatorClient(gql=gql, poll_interval=0).invoke("op", "cap", {}, "k")


def test_factory_planner_selection(monkeypatch):
    monkeypatch.delenv("MISSION_PLANNER_BASE_URL", raising=False)
    assert isinstance(default_planner(), TemplatePlanner)
    monkeypatch.setenv("MISSION_PLANNER_BASE_URL", "http://gpu0:8000/v1")
    monkeypatch.setenv("MISSION_PLANNER_MODEL", "Qwen3-Coder-Next-NVFP4")
    assert isinstance(default_planner(), ModelPlanner)
