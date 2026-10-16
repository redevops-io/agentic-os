"""social.publish through a real MissionRuntime: a human approval (a bare ``{"approved": True}`` marker) executes the
publish only when the content still matches what was staged, and fails closed otherwise."""
from __future__ import annotations

import importlib

import pytest

from agentic_os.app_kit.decision_bridge import MissionRequest
from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import MissionState

core = importlib.import_module("social-autopilot.core")
t_op = importlib.import_module("social-autopilot.test_operator")
build_social_operator = importlib.import_module("social-autopilot.operator").build_social_operator
_FakePsql = t_op._FakePsql


@pytest.fixture(autouse=True)
def _fake_postiz(monkeypatch):
    _FakePsql.inserts, _FakePsql.updates, _FakePsql.queries = [], [], []
    core._CACHE.update(ts=0.0, data=None)
    core._BINDINGS.clear()
    monkeypatch.setattr(core.subprocess, "run", _FakePsql.run)
    monkeypatch.setattr(core, "PG_PASSWORD", "a-real-secret")
    monkeypatch.delenv("APP_DEMO_MODE", raising=False)


def _runtime():
    op = build_social_operator()
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    return MissionRuntime(reg, Executor(LocalOperatorClient({op.name: op})))


def _approve_publish(rt):
    req = MissionRequest(goal="publish the staged post", capability="social.publish",
                         required_capabilities=("social.publish",), inputs={"id": "post-1"}, gated=True,
                         intervention_id="iv-1", opportunity_id="op-1", action_kind="publish", subject="post-1")
    mid = MissionRuntimeLauncher(rt).launch(req)
    rt.run(mid)
    assert rt._missions[mid].state is MissionState.WAITING_HUMAN
    assert _FakePsql.updates == []                       # nothing written before approval
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve")
    return mid


def test_approved_publish_executes_for_staged_content():
    assert core.publish({"id": "post-1"})["status"] == "pending_approval"      # staging records the binding
    rt = _runtime()
    mid = _approve_publish(rt)
    assert any('UPDATE "Post"' in u for u in _FakePsql.updates)           # the approved publish was submitted
    # scheduling is not publication: the read-back stays unverified, so the mission does not claim success
    assert rt._missions[mid].state is not MissionState.SUCCEEDED


def test_approved_publish_refused_when_content_changed_after_staging(monkeypatch):
    core.publish({"id": "post-1"})
    monkeypatch.setattr(t_op, "_POST_ROW_BY_ID", "post-1\x1fQUEUE\x1fint-1\x1f" + '[{"content": "edited after staging"}]')
    rt = _runtime()
    mid = _approve_publish(rt)
    assert rt._missions[mid].state is not MissionState.SUCCEEDED
    assert _FakePsql.updates == []


def test_approved_publish_refused_when_nothing_was_staged():
    rt = _runtime()
    mid = _approve_publish(rt)
    assert rt._missions[mid].state is not MissionState.SUCCEEDED
    assert _FakePsql.updates == []


def test_drafted_post_is_bound_to_its_content():
    d = core.draft({"topic": "market notes"})
    assert d["post_id"] and core._recorded_binding(d["post_id"]) == core._content_hash(d["copy"])

