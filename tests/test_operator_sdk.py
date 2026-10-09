"""Operator SDK + the mock→real bridge: missions driven over the actual HTTP /invoke path
(per-operator TestClients) and over the mission API, plus idempotency on the wire."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.mission.executor import Executor
from agentic_os.mission.operators import HTTPOperatorClient
from agentic_os.mission.operator_sdk import Operator, capability, LocalOperatorClient
from agentic_os.mission import pilots
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.service import build_mission_app
from agentic_os.mission.types import MissionState


# ─── SDK unit: dedupe + HTTP surface ─────────────────────────────────────────
def test_operator_invoke_dedupes_on_idempotency_key():
    op = Operator("x", [capability("x.do", lambda i: {"n": len(i)}, provides=["done"])])
    op.invoke("x.do", {"a": 1}, "k1")
    op.invoke("x.do", {"a": 1}, "k1")          # retry, same key
    assert len(op.calls) == 1                   # handler ran exactly once


def test_operator_router_serves_capabilities_and_invoke():
    op = pilots.build_pilot_operators()["agentic-billing"]
    app = FastAPI(); app.include_router(op.router())
    c = TestClient(app)
    caps = c.get("/capabilities").json()
    assert any(cap["name"] == "billing.create_subscription" for cap in caps["capabilities"])
    r1 = c.post("/invoke", json={"capability": "billing.create_subscription", "inputs": {}},
                headers={"Idempotency-Key": "idem-1"})
    r2 = c.post("/invoke", json={"capability": "billing.create_subscription", "inputs": {}},
                headers={"Idempotency-Key": "idem-1"})
    assert r1.json()["result"]["subscription_id"] == "sub_123"
    assert r1.json() == r2.json() and len(op.calls) == 1     # exactly-once over HTTP


# ─── mission end-to-end over the REAL HTTP operator path ─────────────────────
def _http_client_over_operators(operators: dict[str, Operator]) -> HTTPOperatorClient:
    """Each operator = its own FastAPI app + TestClient; a transport routes by base URL."""
    bases = {name: f"http://{name}" for name in operators}
    clients = {}
    for name, op in operators.items():
        app = FastAPI(); app.include_router(op.router())
        clients[f"http://{name}"] = TestClient(app)

    def transport(url, body, headers, timeout):
        base = next(b for b in clients if url.startswith(b))
        resp = clients[base].post(url[len(base):], json=body, headers=headers)
        return resp.json()

    return HTTPOperatorClient(bases, transport=transport)


def test_onboarding_mission_over_http_invoke_path():
    operators = pilots.build_pilot_operators()
    registry = pilots.build_pilot_registry(operators)
    rt = MissionRuntime(registry, Executor(_http_client_over_operators(operators)))
    m = rt.create_mission("Onboard a new customer", policy_refs=pilots.PILOT_GRANTS,
                          template="onboarding")
    rt.run(m.id)
    assert m.state == MissionState.WAITING_HUMAN               # parked at the compliance gate
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    assert rt._missions[m.id].state == MissionState.SUCCEEDED
    world = rt._world(m.id).snapshot()
    assert {"subscription", "onboarding_sent", "revenue_recorded", "consent_filed"} <= set(world)


# ─── mission API end-to-end (control-plane mount) ────────────────────────────
def test_mission_api_end_to_end():
    c = TestClient(build_mission_app())
    caps = c.get("/capabilities").json()["capabilities"]
    assert any(x["name"] == "compliance.file_consent" for x in caps)
    # discovery by need
    match = c.get("/capabilities", params={"need": "send a welcome message to the customer"}).json()
    assert match["matches"][0]["operator"] == "agentic-support"

    created = c.post("/missions", json={"goal": "Onboard a new customer",
                                        "template": "onboarding", "policy_refs": ["*"]}).json()
    mid = created["id"]
    assert created["state"] == "waiting_human"
    node_id = created["pending_human"]["node_id"]

    done = c.post(f"/missions/{mid}/approve", json={"node_id": node_id, "decision": "approve"}).json()
    assert done["state"] == "succeeded"
    explain = c.get(f"/missions/{mid}/explain").json()
    assert explain["state"] == "succeeded" and len(explain["steps"]) >= 4


def test_invoke_wraps_result_under_declared_output_key():
    """Read-back contract (N4): when a handler returns a dict that lacks the capability's declared
    output key, invoke() wraps it under that key so the verifier can read it back — rather than the
    side effect being REJECTED after it committed. A result that already carries the key is untouched."""
    op = Operator("acct", [
        capability("acct.refund", lambda i: {"status": "staged", "amount": 10},
                   provides=["refund_staged"], outputs={"refund_staged": "the staged refund"},
                   side_effecting=True),
        capability("acct.read", lambda i: {"refund_staged": True, "extra": 1},
                   provides=["refund_staged"], outputs={"refund_staged": "already present"}),
    ])
    merged = op.invoke("acct.refund", {})
    assert merged["refund_staged"] is True                       # declared key MERGED in as a marker
    assert merged["status"] == "staged" and merged["amount"] == 10   # handler's own keys PRESERVED (not nested)
    untouched = op.invoke("acct.read", {})
    assert untouched == {"refund_staged": True, "extra": 1}      # key already present → not touched


def test_invoke_does_not_wrap_an_empty_result():
    """An EMPTY result is left empty so a handler that produced nothing still FAILS read-back (N4):
    wrapping {} under the declared key would make the syntactic verifier vacuous."""
    op = Operator("ref", [capability("ref.badwrite", lambda i: {},
                                     provides=["result"], outputs={"result": "str"}, side_effecting=True)])
    assert op.invoke("ref.badwrite", {}) == {}                   # not wrapped → "result" absent → verifier rejects
