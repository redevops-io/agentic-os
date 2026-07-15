"""Phase 6.0 — build_production_runtime drives the LIVE operator fleet over HTTP.

Each operator is a real FastAPI app (Operator SDK) behind a TestClient; `build_production_runtime`
discovers their capabilities from `GET /capabilities` and drives them via `POST /invoke` — exactly
as production does, with `fetch`/`transport` bound to the TestClients instead of the network. The
onboarding mission then runs end-to-end across four independently-served operators, gated on consent.
"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.mission.operator_sdk import Operator, capability
from agentic_os.mission.production import build_production_runtime, discover, operators_from_modules
from agentic_os.mission.types import MissionState

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]


def _fleet():
    """Four operators (onboarding outcomes), each served as its own FastAPI app + TestClient."""
    ops = {
        "billing": Operator("billing", [
            capability("billing.create_subscription", lambda i: {"subscription_id": "sub_1", "plan": "pro"},
                       provides=["subscription"], side_effecting=True, undo="billing.cancel_subscription",
                       permissions=["billing:write"], estimated_value="high", latency_ms=1200),
            capability("billing.cancel_subscription", lambda i: {"cancelled": True},
                       side_effecting=True, permissions=["billing:write"]),
        ]),
        "support": Operator("support", [
            capability("support.send_onboarding", lambda i: {"onboarding_sent": True},
                       provides=["onboarding_sent"], side_effecting=True, permissions=["support:write"]),
        ]),
        "books": Operator("books", [
            capability("books.record_revenue", lambda i: {"entry": "je_1"},
                       provides=["revenue_recorded"], side_effecting=True, undo="books.reverse_entry",
                       permissions=["books:write"]),
            capability("books.reverse_entry", lambda i: {"reversed": True},
                       side_effecting=True, permissions=["books:write"]),
        ]),
        "compliance": Operator("compliance", [
            capability("compliance.file_consent", lambda i: {"consent_id": "gdpr_1"},
                       provides=["consent_filed"], side_effecting=True, approval_required=True,
                       permissions=["compliance:write"], estimated_value="high"),
        ]),
    }
    clients = {}
    for name, op in ops.items():
        app = FastAPI()
        app.include_router(op.router())
        clients[name] = TestClient(app)
    return clients


def _wire(clients):
    """operator map (name→url) + fetch (GET) + transport (POST) bound to the TestClients."""
    operators = {name: f"http://{name}" for name in clients}

    def fetch(url):
        u = urlparse(url)
        return clients[u.hostname].get(u.path).json()

    def transport(url, body, headers, timeout):
        u = urlparse(url)
        return clients[u.hostname].post(u.path, json=body, headers=headers or {}).json()

    return operators, fetch, transport


def test_discover_registers_the_live_fleet():
    operators, fetch, _ = _wire(_fleet())
    reg, resolve = discover(operators, fetch=fetch)
    names = {c.name for c in reg.all()}
    assert {"billing.create_subscription", "support.send_onboarding",
            "books.record_revenue", "compliance.file_consent"} <= names
    # resolve is keyed by the operator name each manifest DECLARES
    assert resolve["compliance"] == "http://compliance"
    # the consent gate survived the /capabilities round-trip
    consent = next(c for c in reg.all() if c.name == "compliance.file_consent")
    assert consent.approval_required is True and "consent_filed" in consent.provides


def test_operators_from_modules_yaml_shape():
    modules = [{"name": "agentic-billing", "port": 8201}, {"name": "agentic-support", "port": 8202},
               {"name": "sidekick"}]  # no port → skipped
    ops = operators_from_modules(modules, host="10.0.0.5")
    assert ops == {"agentic-billing": "http://10.0.0.5:8201", "agentic-support": "http://10.0.0.5:8202"}


def test_production_runtime_runs_onboarding_over_http():
    operators, fetch, transport = _wire(_fleet())
    rt = build_production_runtime(operators, fetch=fetch, transport=transport)

    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)

    # the consent step gates as a human task — reached over HTTP against the live compliance operator
    assert m.state == MissionState.WAITING_HUMAN
    pending = rt.repo.pending_human(m.id)
    assert pending and pending["capability"] == "compliance.file_consent"

    rt.approve(m.id, pending["node_id"], "approve")
    assert rt._missions[m.id].state == MissionState.SUCCEEDED
    world = rt._world(m.id).snapshot()
    assert {"subscription", "onboarding_sent", "revenue_recorded", "consent_filed"} <= set(world)
