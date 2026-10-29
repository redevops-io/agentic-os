"""The control plane's approval routes: who may read the pending approvals, and who may decide them.

``GET /approvals`` returns every pending approval including its payload (the request text the approver is asked to sign off), so it needs the same
credential as ``POST /approvals/{id}/{decision}``: ``Authorization: Bearer <AGENTIC_OS_CONTROL_TOKEN>`` or ``X-API-Key: <AGENTIC_OS_API_KEY>``. With neither
configured both answer 503 (never open). The permissions admin key is a different privilege and opens neither. A denial runs nothing and does not
echo any approval content. ``/health`` and the other liveness routes stay open (tests/test_kernel_residuals.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest

SECRET_TEXT = "SYNTH-PAYLOAD-777 wire 50000 to account 12345678"


@dataclass
class _Approval:
    id: str
    module: str
    action: str
    summary: str
    status: str = "pending"
    payload: dict | None = None
    findings: list = field(default_factory=list)


class _Context:
    def __init__(self):
        self.items = {"ap1": _Approval("ap1", "billing", "refund", "refund customer 42", payload={"text": SECRET_TEXT})}
        self.resolved = []

    def pending(self):
        return [a for a in self.items.values() if a.status == "pending"]

    def resolve(self, approval_id, approved):
        self.resolved.append((approval_id, approved))
        a = self.items.get(approval_id)
        if a is None or a.status != "pending":
            return None
        a.status = "approved" if approved else "rejected"
        return a


class _Fleet:
    def __init__(self):
        self.context = _Context()
        self.registry = []


@pytest.fixture()
def world(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("cp")
    monkeypatch.setenv("PERMISSIONS_DIR", str(d / "perm"))
    monkeypatch.setenv("AGENTIC_OS_HOME", str(d / "home"))
    from fastapi.testclient import TestClient
    from agentic_os import control_plane
    fleet = _Fleet()
    monkeypatch.setattr(control_plane, "fleet", fleet)
    for k in ("AGENTIC_OS_API_KEY", "AGENTIC_OS_CONTROL_TOKEN", "PERMISSIONS_ADMIN_KEY"):
        monkeypatch.delenv(k, raising=False)
    client = TestClient(control_plane.app, follow_redirects=False)
    return client, fleet, monkeypatch


def _configure(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "control-token-DO-NOT-STORE")
    monkeypatch.setenv("AGENTIC_OS_API_KEY", "fleet-key-DO-NOT-STORE")
    monkeypatch.setenv("PERMISSIONS_ADMIN_KEY", "perm-admin-key-0123456789-DO-NOT-STORE")


def _assert_denied(resp, fleet):
    assert resp.status_code in (401, 503), resp.status_code
    assert SECRET_TEXT not in resp.text and "SYNTH-PAYLOAD" not in resp.text and "refund customer" not in resp.text
    assert all("SYNTH-PAYLOAD" not in v for v in resp.headers.values())
    assert fleet.context.resolved == [], "a denied request must not reach the approval store"
    assert fleet.context.items["ap1"].status == "pending"


# ── GET /approvals ─────────────────────────────────────────────────────────────────────────────
def test_with_no_credential_configured_the_read_is_disabled_not_open(world):
    client, fleet, _ = world
    _assert_denied(client.get("/approvals"), fleet)
    assert client.get("/approvals").status_code == 503


def test_an_anonymous_read_is_refused_and_leaks_no_approval_content(world):
    client, fleet, mp = world
    _configure(mp)
    r = client.get("/approvals")
    assert r.status_code == 401
    _assert_denied(r, fleet)


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer wrong"},
    {"Authorization": "Bearer "},
    {"Authorization": "control-token-DO-NOT-STORE"},                         # no scheme
    {"Authorization": "Basic Y29udHJvbC10b2tlbi1ETy1OT1QtU1RPUkU="},
    {"X-API-Key": "wrong"},
    {"X-API-Key": ""},
    {"X-API-Key": "control-token-DO-NOT-STORE"},                            # the control token is not the fleet key
    {"Authorization": "Bearer fleet-key-DO-NOT-STORE"},                     # the fleet key is not a bearer token
    {"X-API-Key": "perm-admin-key-0123456789-DO-NOT-STORE"},                # insufficient: the permissions admin privilege is separate
    {"Authorization": "Bearer perm-admin-key-0123456789-DO-NOT-STORE"},
])
def test_a_wrong_or_insufficient_credential_is_refused(world, headers):
    client, fleet, mp = world
    _configure(mp)
    _assert_denied(client.get("/approvals", headers=headers), fleet)


def test_a_credential_in_the_query_string_is_not_accepted(world):
    client, fleet, mp = world
    _configure(mp)
    _assert_denied(client.get("/approvals?api_key=fleet-key-DO-NOT-STORE&token=control-token-DO-NOT-STORE"), fleet)


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer control-token-DO-NOT-STORE"},
    {"X-API-Key": "fleet-key-DO-NOT-STORE"},
])
def test_the_right_credential_reads_the_pending_approvals(world, headers):
    client, fleet, mp = world
    _configure(mp)
    r = client.get("/approvals", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert [a["id"] for a in body] == ["ap1"] and body[0]["payload"]["text"] == SECRET_TEXT
    assert fleet.context.resolved == []                                     # reading changes nothing


# ── method and path variants ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("method", ["head", "options", "put", "patch", "delete", "post"])
def test_other_methods_on_the_list_route_never_return_content_or_change_state(world, method):
    client, fleet, mp = world
    _configure(mp)
    r = getattr(client, method)("/approvals")
    assert r.status_code in (401, 405), (method, r.status_code)
    assert SECRET_TEXT not in r.text and fleet.context.resolved == []


@pytest.mark.parametrize("path", ["/approvals/", "/%2Fapprovals", "/approvals%2F", "/approvals/.", "/Approvals", "/approvals;x=1", "/approvals%00"])
def test_path_variants_do_not_bypass_the_credential(world, path):
    client, fleet, mp = world
    _configure(mp)
    r = client.get(path)
    assert SECRET_TEXT not in r.text
    if r.status_code in (301, 302, 307, 308):                               # a redirect to the canonical route carries no content
        assert r.content in (b"", b"null") or SECRET_TEXT.encode() not in r.content
        assert client.get(r.headers["location"]).status_code in (401, 404, 405)
    else:
        assert r.status_code in (401, 404, 405)
    assert fleet.context.resolved == []


# ── POST /approvals/{id}/{decision}: authentication, authorisation, and no side effects on denial ───────────
@pytest.mark.parametrize("headers,expected", [
    ({}, 401),
    ({"Authorization": "Bearer wrong"}, 401),
    ({"X-API-Key": "wrong"}, 401),
    ({"X-API-Key": "perm-admin-key-0123456789-DO-NOT-STORE"}, 401),
])
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_a_denied_decision_changes_nothing(world, headers, expected, decision):
    client, fleet, mp = world
    _configure(mp)
    r = client.post(f"/approvals/ap1/{decision}", headers=headers)
    assert r.status_code == expected
    _assert_denied(r, fleet)


def test_with_no_credential_configured_a_decision_is_disabled(world):
    client, fleet, _ = world
    r = client.post("/approvals/ap1/approve")
    assert r.status_code == 503
    _assert_denied(r, fleet)


@pytest.mark.parametrize("path", ["/approvals/ap1/approve/", "/approvals/ap1/APPROVE", "/approvals/ap1/approve%20", "/approvals//approve",
                                  "/approvals/ap1/approve/extra", "/approvals/ap1"])
def test_decision_path_variants_without_a_credential_change_nothing(world, path):
    client, fleet, mp = world
    _configure(mp)
    r = client.post(path)
    assert r.status_code in (307, 308, 401, 404, 405)
    assert SECRET_TEXT not in r.text and fleet.context.resolved == []
    assert fleet.context.items["ap1"].status == "pending"


def test_a_get_on_the_decision_route_cannot_decide(world):
    client, fleet, mp = world
    _configure(mp)
    r = client.get("/approvals/ap1/approve", headers={"X-API-Key": "fleet-key-DO-NOT-STORE"})
    assert r.status_code in (404, 405) and fleet.context.resolved == []


@pytest.mark.parametrize("decision", ["maybe", "APPROVE", "approve ", "", "approved"])
def test_a_malformed_decision_with_the_right_credential_is_refused_before_the_store(world, decision):
    client, fleet, mp = world
    _configure(mp)
    r = client.post(f"/approvals/ap1/{decision}", headers={"X-API-Key": "fleet-key-DO-NOT-STORE"})
    assert r.status_code in (400, 404, 405)
    assert fleet.context.resolved == [] and fleet.context.items["ap1"].status == "pending"


def test_a_malformed_body_does_not_matter_and_nothing_is_decided_without_a_credential(world):
    client, fleet, mp = world
    _configure(mp)
    r = client.post("/approvals/ap1/approve", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 401
    _assert_denied(r, fleet)


def test_the_right_credential_decides(world):
    client, fleet, mp = world
    _configure(mp)
    assert client.post("/approvals/nope/approve", headers={"X-API-Key": "fleet-key-DO-NOT-STORE"}).status_code == 404
    r = client.post("/approvals/ap1/reject", headers={"Authorization": "Bearer control-token-DO-NOT-STORE"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert ("ap1", False) in fleet.context.resolved


# ── what stays open, and the dashboard ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/health", "/modules", "/overview", "/permissions"])
def test_the_liveness_and_static_routes_stay_open(world, path):
    client, _, mp = world
    _configure(mp)
    assert client.get(path).status_code == 200


def test_the_dashboard_does_not_say_nothing_is_waiting_when_it_is_refused():
    from agentic_os import control_plane
    html = control_plane.DASHBOARD_HTML
    assert 'fetch("/approvals")' in html
    assert "r.status === 401" in html and "r.status === 503" in html
    assert "need the fleet credential" in html
