"""edge-sentinel as a Mission Runtime operator — proof over a fake CrowdSec core.

Exercises the REAL core logic (fetch_activity KPIs + triage, block_ip POSTing a ban
decision to the LAPI) and the SDK-mount contract (GET /capabilities + POST /invoke)
end-to-end, plus the Mission Runtime's own HTTPOperatorClient driving the operator over
the wire with exactly-once idempotency.

The package dir has a HYPHEN (`edge-sentinel`), so it is a namespace package imported via
importlib rather than a plain `import`.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/edge-sentinel/test_operator.py -q
"""
from __future__ import annotations

import importlib
import json
import types
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

core = importlib.import_module("edge-sentinel.core")
_operator = importlib.import_module("edge-sentinel.operator")
build_edge_sentinel_operator = _operator.build_edge_sentinel_operator
from agentic_os.mission.operators import HTTPOperatorClient

# ── a fake CrowdSec core (one active ban + one matching alert) ───────────────
DECISIONS = [{
    "value": "203.0.113.7", "scope": "Ip", "type": "ban",
    "scenario": "crowdsecurity/http-bruteforce", "duration": "3h59m", "origin": "crowdsec",
}]
ALERTS = [{
    "scenario": "crowdsecurity/http-bruteforce",
    "source": {"value": "203.0.113.7", "scope": "Ip"},
    "events_count": 12, "created_at": "2026-07-14T09:00:00Z",
}]


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload if payload is not None else {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_get(url, timeout=None, headers=None, params=None):
    """Module-level httpx.get — used by crowdsec_connected + fetch_decisions."""
    if url.endswith("/v1/decisions"):
        return _Resp(200, DECISIONS)
    return _Resp(200, {})  # /health or / → "up"


class _FakeClient:
    """Stands in for httpx.Client — records the ban POSTs + unban DELETEs to the LAPI."""
    posted: list[tuple] = []
    deleted: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        _FakeClient.posted.append((url, json))
        return _Resp(200, {})

    def delete(self, url, headers=None, params=None):
        _FakeClient.deleted.append((url, params))
        return _Resp(200, {})

    def get(self, url, headers=None, params=None):
        return _Resp(200, {})


def _fake_cscli(args):
    """Stands in for the docker-exec cscli — serves the alert store as JSON."""
    if args[:2] == ["alerts", "list"]:
        return (0, json.dumps(ALERTS), "")
    return (0, "", "")


@pytest.fixture(autouse=True)
def _fake_crowdsec(monkeypatch):
    _FakeClient.posted = []
    _FakeClient.deleted = []
    core._CACHE.update(ts=0.0, data=None)  # no cache bleed between tests
    monkeypatch.setattr(core, "CROWDSEC_BOUNCER_KEY", "test-key")
    monkeypatch.setattr(core, "_cscli", _fake_cscli)
    monkeypatch.setattr(core, "httpx",
                        types.SimpleNamespace(get=_fake_get, Client=lambda timeout=None: _FakeClient()))
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_edge_sentinel_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"sentinel.triage", "sentinel.block_ip", "sentinel.unblock_ip"}
    # remediation gate (modules.yaml approval_required:[remediation]): block_ip WRITES a
    # firewall ban → gated + side-effecting; its saga undo is sentinel.unblock_ip.
    bi = caps["sentinel.block_ip"]
    assert bi["approval_required"] is True
    assert bi["side_effecting"] is True
    assert bi["undo"] == "sentinel.unblock_ip"
    assert "sentinel:write" in bi["permissions"]
    assert "ip_blocked" in bi["provides"]
    # triage reads alerts + explains → read-only, NOT gated.
    assert caps["sentinel.triage"]["approval_required"] is False
    assert caps["sentinel.triage"]["side_effecting"] is False
    assert "threat_posture" in caps["sentinel.triage"]["provides"]
    # unblock_ip is the compensating write (side-effecting, ungated on its own).
    assert caps["sentinel.unblock_ip"]["side_effecting"] is True


def test_triage_is_readonly_and_reads_alerts(client):
    res = client.post("/invoke", json={"capability": "sentinel.triage", "inputs": {}}).json()["result"]
    assert res["action"] == "triage"
    assert res["active_decisions"] == 1          # read the fake LAPI decision
    assert res["alerts"] == 1                     # read the fake cscli alert store
    assert res["severity_breakdown"].get("critical") == 1  # http-bruteforce → critical
    assert _FakeClient.posted == []              # read-only: no writes to CrowdSec


def test_block_ip_posts_a_decision(client):
    r = client.post("/invoke", json={
        "capability": "sentinel.block_ip",
        "inputs": {"ip": "198.51.100.23", "duration": "6h"},
    }).json()
    res = r["result"]
    assert res["status"] == "done" and res["enforced"] is True
    assert res["ip"] == "198.51.100.23" and res["duration"] == "6h"
    # the REAL core POSTed a ban decision to the LAPI decisions endpoint
    assert len(_FakeClient.posted) == 1
    url, payload = _FakeClient.posted[0]
    assert url == "http://localhost:8086/v1/decisions"
    assert payload[0]["value"] == "198.51.100.23" and payload[0]["type"] == "ban"


def test_unblock_ip_deletes_the_decision(client):
    res = client.post("/invoke", json={
        "capability": "sentinel.unblock_ip", "inputs": {"ip": "198.51.100.23"},
    }).json()["result"]
    assert res["status"] == "done" and res["lifted"] is True
    assert len(_FakeClient.deleted) == 1
    url, params = _FakeClient.deleted[0]
    assert url == "http://localhost:8086/v1/decisions"
    assert params == {"ip": "198.51.100.23"}
    assert _FakeClient.posted == []  # unblock does not POST


def test_idempotency_dedupes_the_ban(client):
    body = {"capability": "sentinel.block_ip",
            "inputs": {"ip": "198.51.100.23"}, "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second
    assert len(_FakeClient.posted) == 1  # side effect enforced exactly once


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"edge-sentinel": "http://edge-sentinel"}, transport=_transport)
    tri = oc.invoke("edge-sentinel", "sentinel.triage", {}, idempotency_key="m-1")
    assert tri["action"] == "triage" and tri["active_decisions"] == 1

    blk = oc.invoke("edge-sentinel", "sentinel.block_ip", {"ip": "198.51.100.23"}, idempotency_key="m-2")
    assert blk["status"] == "done" and blk["enforced"] is True
