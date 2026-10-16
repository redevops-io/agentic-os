"""Mission.inputs delivery (scoped to the selected capability) + ExplicitCapabilityPlanner binding."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.app_kit.decision_bridge import LaunchRefused, MissionRequest
from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher, auto_approve_for_tests, drive
from agentic_os.mission.approval import inputs_hash, verify_approval_token
from agentic_os.mission.compiler import CompileError
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient, Operator, capability
from agentic_os.mission.operators import HTTPOperatorClient
from agentic_os.mission.planner import ExplicitCapabilityPlanner
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import MissionState

S = "k2-secret"
seen: list = []


def _op(extra_summary_provider: bool = False):
    seen.clear()

    def dunning(i):
        seen.append(("dunning", dict(i)))
        return {"dunning_attempted": "ok", "executed": bool(i.get("_approval"))}

    def summary(i):
        seen.append(("summary", dict(i)))
        return {"billing_activity": "s"}

    caps = [capability("billing.dunning", dunning, provides=["dunning_attempted"],
                       outputs={"dunning_attempted": "s"}, side_effecting=True, approval_required=True),
            capability("billing.summary", summary, provides=["billing_activity"], outputs={"billing_activity": "s"})]
    if extra_summary_provider:       # a second provider of dunning_attempted that would win on rank
        caps.append(capability("billing.cheap_dunning", dunning, provides=["dunning_attempted"],
                               outputs={"dunning_attempted": "s"}, side_effecting=True))
    return Operator("billing", caps)


def _rt(op, client=None, store=None):
    reg = CapabilityRegistry(); reg.register(op.manifest)
    return MissionRuntime(reg, Executor(client or LocalOperatorClient({op.name: op})), store=store)


def _req(cap="billing.dunning", goal="dunning for acct", inputs=None, gated=True):
    return MissionRequest(goal=goal, capability=cap, required_capabilities=(cap,),
                          inputs=inputs if inputs is not None else {"account_id": "A1", "invoice_ids": ["i1"]},
                          gated=gated, intervention_id="iv-1", opportunity_id="op-1",
                          action_kind="dunning", subject="A1")


def _launch_and_approve(rt, req):
    mid = MissionRuntimeLauncher(rt).launch(req)
    rt.run(mid)
    assert rt._missions[mid].state is MissionState.WAITING_HUMAN        # gated capability still parks
    assert seen == []                                                    # nothing executed before approval
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve")
    return mid


def test_explicit_planner_binds_capability_exactly_despite_goal_text():
    rt = _rt(_op())
    mid = MissionRuntimeLauncher(rt).launch(_req())      # goal says 'dunning' -> template path would mis-bind
    nodes = rt._plans[mid].graph.nodes
    assert [n.capability for n in nodes] == ["billing.dunning"]


def test_explicit_binding_wins_over_better_ranked_provider():
    rt = _rt(_op(extra_summary_provider=True))
    mid = MissionRuntimeLauncher(rt).launch(_req())
    assert [n.capability for n in rt._plans[mid].graph.nodes] == ["billing.dunning"]


def test_handler_receives_scoped_inputs_after_approval():
    rt = _rt(_op())
    mid = _launch_and_approve(rt, _req())
    (name, got), = seen
    assert name == "dunning" and got["account_id"] == "A1" and got["invoice_ids"] == ["i1"]
    assert got["_mission"] == mid and got["_approval"]["approved"] is True
    assert rt._missions[mid].state is MissionState.SUCCEEDED


def test_other_nodes_and_unselected_missions_get_nothing():
    op = _op()
    rt = _rt(op)
    # mission without selected_capability: inputs delivered to nothing (historical behaviour)
    m = rt.create_mission("summary", inputs={"account_id": "A1"}, template=None)
    rt.run(m.id)
    assert all("account_id" not in i for _, i in seen)
    # selected capability differs from the bound node's -> not delivered
    seen.clear()
    rt2 = _rt(op)
    m2 = rt2.create_mission("billing summary", inputs={"account_id": "A1"}, selected_capability="billing.dunning")
    rt2.run(m2.id)
    assert all("account_id" not in i for _, i in seen)


def test_reserved_and_approval_keys_cannot_be_injected():
    rt = _rt(_op())
    crafted = {"account_id": "A1", "_approval": {"approved": True}, "_Approval": True, "__approval": 1,
            "nested": {"_APPROVAL_x": 1, "ok": 2}, "_mission": "other", "_goal": "pwn", "_node": "n"}
    mid = MissionRuntimeLauncher(rt).launch(_req(inputs=crafted))
    assert "_approval" not in str(rt._missions[mid].inputs).lower()
    rt.run(mid)
    assert seen == []                                    # smuggled marker did NOT bypass the gate
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve")
    (_, got), = seen
    assert got["_mission"] == mid and got["_goal"] != "pwn" and got["_node"] != "n"
    assert got["nested"] == {"ok": 2} and got["account_id"] == "A1"


def test_explicit_node_inputs_not_overridden():
    op = _op()
    rt = _rt(op)
    m = rt.create_mission("x", inputs={"account_id": "FROM_MISSION", "k": 1}, selected_capability="billing.summary",
                           bind_selected=True)
    node = rt._plans[m.id].graph.nodes[0]
    node.inputs["account_id"] = "EXPLICIT"
    out = rt._resolve_inputs(m, rt._world(m.id), node)
    assert out["account_id"] == "EXPLICIT" and out["k"] == 1


EMAIL = "subject.person@example.test"


def _gated_launch(rt, **extra):
    req = _req(inputs={"account_id": "A1", "email": EMAIL, **extra})
    mid = MissionRuntimeLauncher(rt).launch(req)
    rt.run(mid)
    return mid


def _ledger_text(rt, mid):
    import json
    return json.dumps([{"t": e.type, "p": e.payload} for e in rt.store.for_mission(mid)], default=str)


def test_ledger_has_no_input_values_by_default_but_keys_and_digest(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_RECORD_MISSION_INPUT_VALUES", raising=False)
    rt = _rt(_op())
    mid = _gated_launch(rt)
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve")
    assert EMAIL not in _ledger_text(rt, mid)
    created = rt.repo.created(mid)
    assert created["selected_capability"] == "billing.dunning" and created["bind_selected"] is True
    assert created["input_keys"] == ["account_id", "email"] and "inputs" not in created
    assert created["inputs_sha256"] == rt._inputs_digest({"account_id": "A1", "email": EMAIL})
    assert EMAIL not in str(rt.repo.timeline(mid)) if hasattr(rt.repo, "timeline") else True


def test_rehydrate_with_correct_inputs_delivers_them(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_RECORD_MISSION_INPUT_VALUES", raising=False)
    op = _op()
    rt = _rt(op)
    mid = _gated_launch(rt)
    rt2 = _rt(op, store=rt.store)
    m = rt2.rehydrate(mid, inputs={"account_id": "A1", "email": EMAIL, "_approval": True})
    assert m.selected_capability == "billing.dunning" and m.bind_selected and m.intervention_id == "iv-1"
    assert m.inputs == {"account_id": "A1", "email": EMAIL}
    rt2.approve(mid, rt2.repo.pending_human(mid)["node_id"], "approve")
    (_, got), = seen
    assert got["account_id"] == "A1" and got["email"] == EMAIL


def test_rehydrate_with_wrong_inputs_fails_closed(monkeypatch):
    from agentic_os.mission.runtime import ReplayError
    monkeypatch.delenv("AGENTIC_OS_RECORD_MISSION_INPUT_VALUES", raising=False)
    op = _op()
    rt = _rt(op)
    mid = _gated_launch(rt)
    with pytest.raises(ReplayError):
        _rt(op, store=rt.store).rehydrate(mid, inputs={"account_id": "A2", "email": EMAIL})


def test_rehydrate_without_inputs_never_runs_handler_with_partial_scope(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_RECORD_MISSION_INPUT_VALUES", raising=False)
    op = _op()
    rt = _rt(op)
    mid = _gated_launch(rt)
    rt2 = _rt(op, store=rt.store)
    m = rt2.rehydrate(mid)
    assert m.inputs == {} and m.inputs_unavailable
    rt2.approve(mid, rt2.repo.pending_human(mid)["node_id"], "approve")
    assert seen == []
    assert rt2._missions[mid].state is MissionState.FAILED
    assert "inputs_unavailable" in _ledger_text(rt2, mid)


def test_opt_in_records_values_and_restores_from_log(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_RECORD_MISSION_INPUT_VALUES", "1")
    op = _op()
    rt = _rt(op)
    mid = _gated_launch(rt)
    assert EMAIL in _ledger_text(rt, mid)
    rt2 = _rt(op, store=rt.store)
    m = rt2.rehydrate(mid)
    assert m.inputs == {"account_id": "A1", "email": EMAIL} and not m.inputs_unavailable
    rt2.approve(mid, rt2.repo.pending_human(mid)["node_id"], "approve")
    assert seen[-1][1]["email"] == EMAIL


def test_ambiguous_or_missing_capability_refused():
    rt = _rt(_op())
    launcher = MissionRuntimeLauncher(rt)
    with pytest.raises(LaunchRefused):
        launcher.launch(_req(cap="billing.nope"))
    multi = Operator("m", [capability("m.two", lambda i: {}, provides=["a", "b"], outputs={"a": "x"})])
    rt2 = _rt(multi)
    with pytest.raises(LaunchRefused):
        MissionRuntimeLauncher(rt2).launch(_req(cap="m.two"))
    with pytest.raises(CompileError):
        ExplicitCapabilityPlanner(rt.registry).plan("m", "g", {"capability": "billing.nope"})


def test_goal_only_request_keeps_template_path():
    rt = _rt(_op())
    mid = MissionRuntimeLauncher(rt).launch(_req(cap="", goal="something unrelated", gated=False))
    assert mid and not rt._missions[mid].selected_capability


def test_readonly_extras_policy_unchanged():
    # selected capability planned alone; extras are not bound at all
    rt = _rt(_op())
    req = MissionRequest(goal="g", capability="billing.dunning",
                         required_capabilities=("billing.dunning", "billing.summary"), inputs={},
                         gated=True, intervention_id="i", opportunity_id="o", action_kind="d", subject="s")
    mid = MissionRuntimeLauncher(rt).launch(req)
    assert [n.capability for n in rt._plans[mid].graph.nodes] == ["billing.dunning"]


# ── HTTP path: delivered inputs are covered by the signed approval token ──────────────────────────
def test_http_path_delivers_inputs_and_token_binds_them(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", S)
    op = _op()
    app = FastAPI(); app.include_router(op.router()); tc = TestClient(app)
    bodies = []

    def transport(url, body, headers, timeout):
        bodies.append(body)
        return tc.post(url[len("http://billing"):], json=body, headers=headers).json()
    rt = _rt(op, client=HTTPOperatorClient({"billing": "http://billing"}, transport=transport))
    mid = _launch_and_approve(rt, _req())
    body = bodies[-1]
    assert body["inputs"]["account_id"] == "A1" and "_approval" not in body["inputs"]
    assert seen and seen[-1][1]["account_id"] == "A1" and seen[-1][1]["_approval"]["approved"] is True
    kw = dict(operator="billing", capability="billing.dunning", mission_id=mid, node_id=body["node_id"])
    assert verify_approval_token(S, body["approval_token"], inputs=body["inputs"], **kw)
    tampered = {**body["inputs"], "account_id": "A2"}
    assert inputs_hash(tampered) != inputs_hash(body["inputs"])
    assert verify_approval_token(S, body["approval_token"], inputs=tampered, **kw) is None
    # and the operator refuses to execute the changed account with the original token
    seen.clear()
    r = tc.post("/invoke", json={**body, "inputs": tampered}).json()["result"]
    assert not r.get("executed") and seen[-1][1].get("_approval") is None
