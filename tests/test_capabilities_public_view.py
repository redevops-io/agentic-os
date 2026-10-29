"""``GET /capabilities`` (mission routers, mounted on the control plane): an anonymous caller gets a trimmed catalogue (name, operator, what it provides); the detail is for a
caller that presents a valid control or tenant token. Hidden from the anonymous view: inputs and outputs, cost, permissions, side effects and undo or compensation,
approval flags, secrets, network and authority requirements, isolation and concurrency, resource keys, data classes, confidence and the embedding."""
from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

TOKEN = "control-token-0123456789-DO-NOT-STORE"
PUBLIC_KEYS = {"name", "operator", "provides"}
HIDDEN = {"inputs", "outputs", "cost", "permissions", "side_effecting", "undo", "compensation", "approval_required", "secrets", "network", "required_authority",
          "isolation_class", "resource_keys", "data_classifications", "confidence", "embedding", "irreversible", "deterministic", "estimated_value"}


@pytest.fixture()
def client(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("cp")
    monkeypatch.setenv("PERMISSIONS_DIR", str(d / "perm"))
    monkeypatch.setenv("AGENTIC_OS_HOME", str(d / "home"))
    monkeypatch.delenv("AGENTIC_OS_GATE_READS", raising=False)
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", TOKEN)
    from agentic_os import control_plane
    return TestClient(control_plane.app, follow_redirects=False)


def test_an_anonymous_caller_gets_only_names_operators_and_what_they_provide(client):
    r = client.get("/capabilities")
    assert r.status_code == 200
    caps = r.json()["capabilities"]
    assert caps and all(set(c) == PUBLIC_KEYS for c in caps)
    assert not (HIDDEN & {k for c in caps for k in c})
    body = r.text
    for word in ("embedding", "usd", "latency_ms", "billing:write", "required_authority"):
        assert word not in body, word


def test_discovery_by_need_does_not_return_scores_to_an_anonymous_caller(client):
    r = client.get("/capabilities", params={"need": "subscription", "k": 3}).json()
    assert r["matches"] and all(set(m) == {"capability", "operator"} for m in r["matches"])


def test_a_valid_token_gets_the_detail(client):
    caps = client.get("/capabilities", headers={"Authorization": "Bearer " + TOKEN}).json()["capabilities"]
    assert caps and all(HIDDEN <= set(c) | {"undo"} for c in caps[:1]) and all("cost" in c and "permissions" in c and "embedding" in c for c in caps)
    m = client.get("/capabilities", params={"need": "subscription"}, headers={"Authorization": "Bearer " + TOKEN}).json()["matches"]
    assert m and all("score" in x for x in m)


@pytest.mark.parametrize("auth", ["Bearer wrong", "Bearer ", "Basic abc"])
def test_a_wrong_token_is_refused_not_treated_as_anonymous(client, auth):
    assert client.get("/capabilities", headers={"Authorization": auth}).status_code == 401


def test_a_dashboard_or_fleet_credential_does_not_unlock_the_detail(client, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_API_KEY", "fleet-key-0123456789-DO-NOT-STORE")
    monkeypatch.setenv("AGENTIC_OS_DASHBOARD_TOKEN", "dash-token-0123456789-abcdefghij-DO-NOT-STORE")
    for h in ({"X-API-Key": "fleet-key-0123456789-DO-NOT-STORE"}, {"X-Dashboard-Token": "dash-token-0123456789-abcdefghij-DO-NOT-STORE"}):
        caps = client.get("/capabilities", headers=h).json()["capabilities"]
        assert all(set(c) == PUBLIC_KEYS for c in caps)


def test_the_local_demo_opt_out_of_read_gating_keeps_the_detail(client, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_GATE_READS", "0")
    caps = client.get("/capabilities").json()["capabilities"]
    assert all("cost" in c for c in caps)
