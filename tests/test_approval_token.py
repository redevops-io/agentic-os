"""Signed approval tokens: unit tests + end-to-end over the HTTP operator path."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.mission import pilots
from agentic_os.mission.approval import (ReplayGuard, inputs_hash, mint_approval_token,
                                         verify_approval_token)
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import Operator, capability
from agentic_os.mission.operators import HTTPOperatorClient
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import MissionState

S = "s3cret"
KW = dict(operator="op", capability="refund", mission_id="m1", node_id="n1")
MARK = {"approved": True}


def _mint(inputs=None, **o):
    return mint_approval_token(S, **{**KW, **o}, inputs=inputs or {"inv": "INV-1", "amount": 10}, marker=MARK)


def _verify(tok, inputs=None, secret=S, **o):
    a = dict(operator="op", capability="refund")
    a.update(o)
    return verify_approval_token(secret, tok, inputs=inputs or {"inv": "INV-1", "amount": 10}, **a)


def test_roundtrip_ignores_runtime_meta_keys():
    tok = _mint()
    assert _verify(tok, {"inv": "INV-1", "amount": 10, "_mission": "m1", "_goal": "g", "_node": "n1"}) == MARK


def test_tampered_payload_and_signature():
    p, s = _mint().split(".")
    assert _verify(p[:-2] + ("AA" if p[-2:] != "AA" else "BB") + "." + s) is None
    assert _verify(p + "." + s[:-2] + ("AA" if s[-2:] != "AA" else "BB")) is None
    assert _verify("garbage") is None and _verify("a.b") is None and _verify(None) is None and _verify(5) is None


def test_expired():
    tok = _mint(ttl_s=10, now=1000.0)
    assert _verify(tok, now=1005.0) == MARK
    assert _verify(tok, now=1011.0) is None


def test_wrong_capability_operator_secret():
    tok = _mint()
    assert _verify(tok, capability="other") is None
    assert _verify(tok, operator="other") is None
    assert _verify(tok, secret="nope") is None
    assert _verify(tok, mission_id="m2") is None and _verify(tok, node_id="n2") is None


def test_changed_inputs():
    tok = _mint()
    assert _verify(tok, {"inv": "INV-2", "amount": 10}) is None
    assert _verify(tok, {"inv": "INV-1", "amount": 999}) is None
    assert inputs_hash({"a": 1, "_Approval": 1, "__approval_x": 2}) == inputs_hash({"a": 1})


def test_replay_rejected():
    g = ReplayGuard()
    tok = _mint()
    assert _verify(tok, replay_guard=g) == MARK
    assert _verify(tok, replay_guard=g) is None


def test_replay_guard_bounded_fails_closed():
    g = ReplayGuard(max_entries=2)
    assert g.consume("a", 100, now=0) and g.consume("b", 100, now=0)
    assert not g.consume("c", 100, now=0)
    assert g.consume("c", 100, now=101)          # expired entries are reclaimed


def test_no_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_APPROVAL_SECRET", raising=False)
    assert mint_approval_token(None, **KW, inputs={}, marker=MARK) is None
    assert mint_approval_token("", **KW, inputs={}, marker=MARK) is None
    assert verify_approval_token(None, _mint(), operator="op", capability="refund",
                                 inputs={"inv": "INV-1", "amount": 10}) is None


def test_secret_read_at_call_time(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", "late")
    tok = mint_approval_token(None, **KW, inputs={}, marker=MARK)
    assert tok and verify_approval_token(None, tok, operator="op", capability="refund", inputs={}) == MARK


# ─── /invoke forgery + token handling ────────────────────────────────────────
def _gated_op():
    seen = []

    def refund(i):
        seen.append(dict(i))
        return {"status": "done", "executed": True} if i.get("_approval") else {"status": "pending_approval"}
    op = Operator("op", [capability("refund", refund, side_effecting=True, outputs={"refunded": "x"})])
    app = FastAPI(); app.include_router(op.router())
    return op, TestClient(app), seen


@pytest.mark.parametrize("inputs", [
    {"_approval": True}, {"_Approval": {"approved": True}}, {"__approval": True},
    {"x": {"_approval": True}}, {"_APPROVAL_edit": 1}, {"l": [{"_approval": 1}]}])
def test_forged_markers_never_reach_handler(inputs):
    op, c, seen = _gated_op()
    r = c.post("/invoke", json={"capability": "refund", "inputs": inputs, "approval_token": "junk"})
    assert r.status_code == 200 and r.json()["result"]["status"] == "pending_approval"
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                assert "approval" not in k.lower(); walk(v)
        elif isinstance(o, list):
            [walk(v) for v in o]
    walk(seen[0])


def test_valid_token_injects_marker_once_and_binds_inputs(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", S)
    op, c, seen = _gated_op()
    ins = {"inv": "INV-1", "amount": 10}
    tok = mint_approval_token(S, operator="op", capability="refund", mission_id=None, node_id=None,
                              inputs=ins, marker=MARK)
    body = lambda i, t, k: {"capability": "refund", "inputs": i, "approval_token": t, "idempotency_key": k}
    assert c.post("/invoke", json=body({"inv": "INV-1", "amount": 999}, tok, "k1")).json()["result"]["status"] == "pending_approval"
    assert c.post("/invoke", json=body(ins, tok, "k2")).json()["result"]["executed"] is True
    assert c.post("/invoke", json=body(ins, tok, "k3")).json()["result"]["status"] == "pending_approval"  # replay


# ─── end-to-end: mission over HTTPOperatorClient ─────────────────────────────
def _e2e():
    ran = {"executed": 0, "staged": 0}

    def file_consent(i):
        if i.get("_approval"):
            ran["executed"] += 1
            return {"consent_id": "gdpr_42", "executed": True}
        ran["staged"] += 1
        return {"status": "pending_approval", "executed": False}
    operators = pilots.build_pilot_operators()
    operators["agentic-compliance"] = Operator("agentic-compliance", [
        capability("compliance.file_consent", file_consent, provides=["consent_filed"],
                   outputs={"consent_id": "string"}, side_effecting=True, approval_required=True,
                   permissions=["compliance:write"])])
    clients = {}
    for name, op in operators.items():
        app = FastAPI(); app.include_router(op.router()); clients[f"http://{name}"] = TestClient(app)

    def transport(url, body, headers, timeout):
        base = next(b for b in clients if url.startswith(b))
        return clients[base].post(url[len(base):], json=body, headers=headers).json()
    client = HTTPOperatorClient({n: f"http://{n}" for n in operators}, transport=transport)
    rt = MissionRuntime(pilots.build_pilot_registry(operators), Executor(client))
    m = rt.create_mission("Onboard a new customer", policy_refs=pilots.PILOT_GRANTS, template="onboarding")
    rt.run(m.id)
    return rt, m, ran


def test_e2e_http_approval_executes_exactly_once(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", S)
    rt, m, ran = _e2e()
    assert m.state == MissionState.WAITING_HUMAN and ran["executed"] == 0
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    assert rt._missions[m.id].state == MissionState.SUCCEEDED
    assert ran["executed"] == 1
    assert "consent_filed" in rt._world(m.id).snapshot()


def test_e2e_http_approval_without_secret_stays_staged(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_APPROVAL_SECRET", raising=False)
    rt, m, ran = _e2e()
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    assert ran["executed"] == 0                          # fail closed: never executes without a secret
    assert rt._missions[m.id].state != MissionState.SUCCEEDED


# ─── saga rollback: a GATED undo must execute during compensation, not stage ──
class _SagaPlanner:
    def plan(self, mission_id, goal, context):
        from agentic_os.mission.types import ExecutionIntent, IntentStep
        return ExecutionIntent(mission_id=mission_id, steps=[
            IntentStep(outcome="charged", need="charge the customer"),
            IntentStep(outcome="notified", need="notify the customer", inputs_from=["charged"]),
        ])


def _saga_http(monkeypatch, secret):
    if secret:
        monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", S)
    else:
        monkeypatch.delenv("AGENTIC_OS_APPROVAL_SECRET", raising=False)
    seen = {"refund_executed": 0, "refund_staged": 0}

    def refund(i):                                        # the UNDO is itself approval-gated
        if i.get("_approval"):
            seen["refund_executed"] += 1
            return {"refunded": True, "executed": True}
        seen["refund_staged"] += 1
        return {"status": "pending_approval", "executed": False}

    def notify(i):
        raise RuntimeError("smtp down")                   # the later step fails -> saga unwinds
    operators = {
        "billing": Operator("billing", [
            capability("charge", lambda i: {"charge_id": "ch_1"}, provides=["charged"],
                       side_effecting=True, undo="refund", permissions=["*"]),
            capability("refund", refund, side_effecting=True, approval_required=True, permissions=["*"])]),
        "email": Operator("email", [
            capability("notify", notify, provides=["notified"], side_effecting=True, permissions=["*"])]),
    }
    clients = {}
    for name, op in operators.items():
        app = FastAPI(); app.include_router(op.router())
        clients[f"http://{name}"] = TestClient(app, raise_server_exceptions=False)

    def transport(url, body, headers, timeout):
        base = next(b for b in clients if url.startswith(b))
        r = clients[base].post(url[len(base):], json=body, headers=headers)
        if r.status_code >= 500:
            raise RuntimeError(f"operator error {r.status_code}")
        return r.json()
    client = HTTPOperatorClient({n: f"http://{n}" for n in operators}, transport=transport)
    rt = MissionRuntime(pilots.build_pilot_registry(operators), Executor(client), planner=_SagaPlanner())
    m = rt.create_mission("charge and notify", policy_refs=["*"])
    rt.run(m.id)
    return rt, m, seen


def test_e2e_http_saga_gated_undo_executes(monkeypatch):
    rt, m, seen = _saga_http(monkeypatch, secret=True)
    assert m.state == MissionState.FAILED
    assert seen == {"refund_executed": 1, "refund_staged": 0}      # rolled back for real over HTTP
    comp = [e for e in rt.repo.timeline(m.id) if e["type"] == "NodeCompensated"]
    assert len(comp) == 1 and comp[0]["payload"]["result"].get("executed") is True


def test_e2e_http_saga_gated_undo_without_secret_stays_visibly_unexecuted(monkeypatch):
    rt, m, seen = _saga_http(monkeypatch, secret=False)
    assert seen["refund_executed"] == 0                             # fail closed: no secret, no execution
    tl = rt.repo.timeline(m.id)
    assert not [e for e in tl if e["type"] == "NodeCompensated"]            # a staged undo is NOT "compensated"
    comp = [e for e in tl if e["type"] == "NodeCompensationFailed"]
    assert comp and comp[0]["payload"]["result"].get("executed") is False   # the staged result is visible
    assert m.outcome["compensation_incomplete"] is True
