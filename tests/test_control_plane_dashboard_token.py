"""The read-only dashboard token (``AGENTIC_OS_DASHBOARD_TOKEN``, header ``X-Dashboard-Token``).

It lets the dashboard page read what it shows (``GET /api/fleet``, ``/approvals``, ``/jobs/{id}``, ``/status``, ``/notify/status``) without holding the fleet key, which can
dispatch, start, stop and approve. It is a separate secret: at least 24 characters, different from the fleet key, the control token and the permissions admin key, compared in
constant time. It opens exactly those five GET routes and nothing else: not a mutating route, not the security block of ``/health``, not the mission routers, not the
permissions admin API, not ``/openapi.json``. The existing fleet credentials keep working on everything they worked on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

DASH = "dash-token-0123456789-abcdefghij-DO-NOT-STORE"
FLEET_KEY = "fleet-key-0123456789-DO-NOT-STORE"
CONTROL = "control-token-0123456789-DO-NOT-STORE"
PERM = "perm-admin-key-0123456789-DO-NOT-STORE"
SECRET = "SYNTH-DASH-CONTENT-DO-NOT-STORE"

READS = ["/api/fleet", "/approvals", "/jobs/job-00001", "/status", "/notify/status"]


@dataclass
class _Approval:
    id: str = "ap1"
    module: str = "billing"
    action: str = "refund"
    summary: str = SECRET
    status: str = "pending"
    payload: dict = field(default_factory=lambda: {"text": SECRET})
    findings: list = field(default_factory=list)


class _Context:
    def __init__(self, calls):
        self.calls = calls
        self.notifier = SimpleNamespace(channels=[SimpleNamespace(name="chat")])
        self.items = [_Approval()]

    def pending(self):
        return list(self.items)

    def resolve(self, approval_id, approved):
        self.calls.append(("resolve", approval_id, approved))
        return None


class _Registry(list):
    def get(self, name):
        return self[0]


class _Fleet:
    def __init__(self):
        self.calls = []
        self.registry = _Registry([SimpleNamespace(name="m1", repo="o/r", pain="p", tagline="t", agents=["a"], approval_required=[], deploy="compose", port=1)])
        self.context = _Context(self.calls)
        self.authority = None

    def job(self, job_id):
        return {"status": "done", "module": "m1", "action": "x", "result": SECRET} if job_id == "job-00001" else None

    def dispatch(self, *a, **k):
        self.calls.append(("dispatch", a, k))
        raise KeyError("stub")

    def up(self, *a):
        self.calls.append(("up", a))
        return []

    def down(self, *a):
        self.calls.append(("down", a))
        return []


@pytest.fixture()
def world(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("cp")
    monkeypatch.setenv("PERMISSIONS_DIR", str(d / "perm"))
    monkeypatch.setenv("AGENTIC_OS_HOME", str(d / "home"))
    from agentic_os import control_plane
    fleet = _Fleet()
    monkeypatch.setattr(control_plane, "fleet", fleet)

    async def _probe(client, m):
        return {"health": "up", "core": None, "connected": None}
    monkeypatch.setattr(control_plane, "_probe_health", _probe)
    monkeypatch.setattr(control_plane, "_gateway", SimpleNamespace(channels=[]))
    for k in ("AGENTIC_OS_API_KEY", "AGENTIC_OS_CONTROL_TOKEN", "PERMISSIONS_ADMIN_KEY", "AGENTIC_OS_DASHBOARD_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    control_plane._dashboard_token_warned.clear()
    return TestClient(control_plane.app, follow_redirects=False), fleet, monkeypatch, control_plane


def _all(mp, dash=DASH):
    mp.setenv("AGENTIC_OS_API_KEY", FLEET_KEY)
    mp.setenv("AGENTIC_OS_CONTROL_TOKEN", CONTROL)
    mp.setenv("PERMISSIONS_ADMIN_KEY", PERM)
    if dash is not None:
        mp.setenv("AGENTIC_OS_DASHBOARD_TOKEN", dash)


# ── what it opens ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", READS)
def test_the_dashboard_token_reads_each_of_the_five_routes(world, path):
    client, fleet, mp, _ = world
    _all(mp)
    r = client.get(path, headers={"X-Dashboard-Token": DASH})
    assert r.status_code == 200, (path, r.status_code)
    assert fleet.calls == []


def test_the_other_credentials_still_read_the_same_routes(world):
    client, _, mp, _ = world
    _all(mp)
    for path in READS:
        assert client.get(path, headers={"X-API-Key": FLEET_KEY}).status_code == 200
        assert client.get(path, headers={"Authorization": "Bearer " + CONTROL}).status_code == 200


@pytest.mark.parametrize("path", READS)
@pytest.mark.parametrize("headers", [
    {"X-Dashboard-Token": "wrong"},
    {"X-Dashboard-Token": ""},
    {"X-Dashboard-Token": DASH[:-1]},
    {"X-Dashboard-Token": DASH + "x"},
    {"X-API-Key": DASH},                                  # the dashboard token is not the fleet key
    {"Authorization": "Bearer " + DASH},                  # nor a bearer token
    {"X-Dashboard-Token": FLEET_KEY},                     # the fleet key is not the dashboard token
    {"X-Dashboard-Token": CONTROL},
    {"X-Dashboard-Token": PERM},
])
def test_a_wrong_or_crossed_credential_is_refused_on_the_five_routes(world, path, headers):
    client, fleet, mp, _ = world
    _all(mp)
    r = client.get(path, headers=headers)
    assert r.status_code == 401 and SECRET not in r.text
    assert fleet.calls == []


def test_a_dashboard_token_in_the_query_string_is_ignored(world):
    client, _, mp, _ = world
    _all(mp)
    assert client.get(f"/api/fleet?x_dashboard_token={DASH}&dashboard_token={DASH}").status_code == 401


def test_only_the_dashboard_token_configured_the_reads_work_and_everything_else_is_401(world):
    client, _, mp, _ = world
    mp.setenv("AGENTIC_OS_DASHBOARD_TOKEN", DASH)
    assert client.get("/api/fleet", headers={"X-Dashboard-Token": DASH}).status_code == 200
    assert client.get("/api/fleet").status_code == 401 and client.get("/approvals", headers={"X-Dashboard-Token": "no"}).status_code == 401
    assert client.post("/dispatch", json={}, headers={"X-Dashboard-Token": DASH}).status_code == 503       # no fleet credential configured: disabled
    assert client.get("/openapi.json", headers={"X-Dashboard-Token": DASH}).status_code == 503


# ── what it must not open: the drift test over EVERY other route ─────────────────────────────────
def _urls(path):
    return (path.replace("{approval_id}", "ap1").replace("{decision}", "approve").replace("{job_id}", "job-00001").replace("{gid}", "g1")
            .replace("{mid}", "m1").replace("{action}", "undo").replace("{name}", "m1").replace("{path:path}", "x"))


def test_the_dashboard_token_is_refused_on_every_route_other_than_the_five(world):
    client, fleet, mp, cp = world
    _all(mp)
    from tests.test_control_plane_route_table import EXPECTED, FLEET_READ, PUBLIC, PROXY
    refused = []
    for (method, path), cls in sorted(EXPECTED.items()):
        if cls in (FLEET_READ, PUBLIC, PROXY):
            continue                                                  # the five it may open; and routes that are open to everyone anyway
        for hdr in ({"X-Dashboard-Token": DASH}, {"X-API-Key": DASH}, {"Authorization": "Bearer " + DASH}):
            r = client.request(method, _urls(path), headers=hdr, json={} if method in ("POST", "PUT", "PATCH") else None)
            assert r.status_code in (401, 403), (method, path, hdr, r.status_code)
            assert SECRET not in r.text
        refused.append((method, path))
    assert len(refused) >= 20
    assert fleet.calls == [], f"the dashboard token reached the fleet: {fleet.calls}"


def test_the_dashboard_token_does_not_unlock_the_security_block_of_health(world):
    client, _, mp, _ = world
    _all(mp)
    body = client.get("/health", headers={"X-Dashboard-Token": DASH}).json()
    assert set(body) == {"status", "modules"}
    assert "security" in client.get("/health", headers={"X-API-Key": FLEET_KEY}).json()


def test_the_dashboard_token_does_not_unlock_the_capability_detail(world):
    client, _, mp, _ = world
    _all(mp)
    r = client.get("/capabilities", headers={"X-Dashboard-Token": DASH}).json()["capabilities"]
    assert r and all(set(c) == {"name", "operator", "provides"} for c in r)
    assert client.get("/capabilities", headers={"Authorization": "Bearer " + DASH}).status_code == 401


# ── the token itself ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("token,why", [("short-token", "shorter"), ("23-characters-xxxxxxxxx", "shorter"), (FLEET_KEY, "equal"), (CONTROL, "equal"), (PERM, "equal")])
def test_an_unusable_dashboard_token_is_disabled_and_logged_once(world, caplog, token, why):
    client, _, mp, _ = world
    assert len(token) < 24 or token in (FLEET_KEY, CONTROL, PERM)
    _all(mp, dash=token)
    with caplog.at_level(logging.ERROR, logger="agentic_os.control_plane"):
        for _ in range(3):
            assert client.get("/api/fleet", headers={"X-Dashboard-Token": token}).status_code in (200, 401)
    msgs = [r.getMessage() for r in caplog.records if "dashboard token" in r.getMessage()]
    assert len(msgs) == 1 and token not in msgs[0]                    # logged once, without the value
    # it opens nothing as a dashboard token (an equal value works only because it IS the fleet credential, as the X-API-Key / bearer)
    if why == "shorter":
        assert client.get("/api/fleet", headers={"X-Dashboard-Token": token}).status_code == 401


def test_the_comparison_is_constant_time():
    import inspect
    from agentic_os import control_plane
    from agentic_os.mission import control_auth
    assert "token_matches(x_dashboard_token, dash)" in inspect.getsource(control_plane.require_fleet_read)
    assert "hmac.compare_digest" in inspect.getsource(control_auth.token_matches)
