"""The control plane's fleet READ routes need the fleet credential: ``GET /jobs/{id}``, ``GET /api/fleet``, ``GET /status`` and ``GET /notify/status``.

They expose the result text and errors of dispatched agent actions (sequential, guessable job ids), the application inventory and topology (repositories,
ports, approval-required capability names, backend names, live health), which modules are up, and which chat channels are wired. They take the same credential
as the other fleet routes: ``Authorization: Bearer <AGENTIC_OS_CONTROL_TOKEN>`` or ``X-API-Key: <AGENTIC_OS_API_KEY>``; nothing configured answers 503; the
permissions admin key is a different privilege and does not open them. A denial runs nothing and returns no content. The liveness and static routes stay open.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

SECRET_RESULT = "SYNTH-JOB-RESULT-777 refunded account 12345678"
SECRET_TOPOLOGY = "SYNTH-FLEET-TOPOLOGY-DO-NOT-STORE"
SECRET_CHANNEL = "SYNTH-CHANNEL-555"

FLEET_KEY = "fleet-key-DO-NOT-STORE"
CONTROL = "control-token-DO-NOT-STORE"
PERM_ADMIN = "perm-admin-key-0123456789-DO-NOT-STORE"

ROUTES = ["/jobs/job-00001", "/api/fleet", "/status", "/notify/status"]


class _Registry:
    def __init__(self, calls):
        self.calls = calls
        self.mods = [SimpleNamespace(name="demo-module", repo="org/" + SECRET_TOPOLOGY, pain=SECRET_TOPOLOGY, tagline=SECRET_TOPOLOGY, agents=[SECRET_TOPOLOGY],
                                     approval_required=[SECRET_TOPOLOGY], deploy="compose", port=18999)]

    def __iter__(self):
        self.calls.append("registry")
        return iter(self.mods)

    def __len__(self):
        return len(self.mods)

    def get(self, name):
        return self.mods[0]


class _Fleet:
    def __init__(self):
        self.calls = []
        self.registry = _Registry(self.calls)
        self.context = SimpleNamespace(notifier=SimpleNamespace(channels=[SimpleNamespace(name=SECRET_CHANNEL)]))

    def job(self, job_id):
        self.calls.append(("job", job_id))
        return {"status": "done", "module": "demo-module", "action": "refund", "result": SECRET_RESULT} if job_id == "job-00001" else None


@pytest.fixture()
def world(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("cp")
    monkeypatch.setenv("PERMISSIONS_DIR", str(d / "perm"))
    monkeypatch.setenv("AGENTIC_OS_HOME", str(d / "home"))
    from agentic_os import control_plane
    fleet = _Fleet()
    monkeypatch.setattr(control_plane, "fleet", fleet)
    probes = []

    async def _probe(client, m):
        probes.append(m.name)
        return {"health": "up", "core": SECRET_TOPOLOGY, "connected": True}
    monkeypatch.setattr(control_plane, "_probe_health", _probe)
    monkeypatch.setattr(control_plane, "_gateway", SimpleNamespace(channels=[SECRET_CHANNEL]))
    for k in ("AGENTIC_OS_API_KEY", "AGENTIC_OS_CONTROL_TOKEN", "PERMISSIONS_ADMIN_KEY"):
        monkeypatch.delenv(k, raising=False)
    return TestClient(control_plane.app, follow_redirects=False), fleet, probes, monkeypatch


def _configure(mp):
    mp.setenv("AGENTIC_OS_CONTROL_TOKEN", CONTROL)
    mp.setenv("AGENTIC_OS_API_KEY", FLEET_KEY)
    mp.setenv("PERMISSIONS_ADMIN_KEY", PERM_ADMIN)


def _no_content(resp):
    blob = resp.text + " ".join(f"{k}:{v}" for k, v in resp.headers.items())
    for needle in ("SYNTH-", "refunded", "demo-module", "18999", "org/"):
        assert needle not in blob, (needle, blob[:200])


def _untouched(fleet, probes):
    assert fleet.calls == [] and probes == [], f"a denied request reached the fleet: {fleet.calls} {probes}"


@pytest.mark.parametrize("path", ROUTES)
def test_with_no_credential_configured_the_read_is_disabled_not_open(world, path):
    client, fleet, probes, _ = world
    r = client.get(path)
    assert r.status_code == 503
    _no_content(r)
    _untouched(fleet, probes)


@pytest.mark.parametrize("path", ROUTES)
def test_an_anonymous_read_is_refused(world, path):
    client, fleet, probes, mp = world
    _configure(mp)
    r = client.get(path)
    assert r.status_code == 401
    _no_content(r)
    _untouched(fleet, probes)


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer wrong"},
    {"Authorization": "Bearer "},
    {"Authorization": CONTROL},                                              # no scheme
    {"Authorization": "Basic Y29udHJvbC10b2tlbi1ETy1OT1QtU1RPUkU="},
    {"X-API-Key": "wrong"},
    {"X-API-Key": ""},
    {"X-API-Key": CONTROL},                                                  # the control token is not the fleet key
    {"Authorization": "Bearer " + FLEET_KEY},                                # the fleet key is not a bearer token
    {"X-API-Key": PERM_ADMIN},                                               # insufficient: the permissions admin privilege is separate
    {"Authorization": "Bearer " + PERM_ADMIN},
])
@pytest.mark.parametrize("path", ROUTES)
def test_a_wrong_or_insufficient_credential_is_refused(world, path, headers):
    client, fleet, probes, mp = world
    _configure(mp)
    r = client.get(path, headers=headers)
    assert r.status_code == 401
    _no_content(r)
    _untouched(fleet, probes)


@pytest.mark.parametrize("path", ROUTES)
def test_a_credential_in_the_query_string_is_ignored(world, path):
    client, fleet, probes, mp = world
    _configure(mp)
    sep = "&" if "?" in path else "?"
    r = client.get(f"{path}{sep}api_key={FLEET_KEY}&token={CONTROL}&access_token={CONTROL}")
    assert r.status_code == 401
    _no_content(r)
    _untouched(fleet, probes)


@pytest.mark.parametrize("headers", [{"Authorization": "Bearer " + CONTROL}, {"X-API-Key": FLEET_KEY}])
def test_the_right_credential_reads_each_route(world, headers):
    client, fleet, probes, mp = world
    _configure(mp)
    r = client.get("/jobs/job-00001", headers=headers)
    assert r.status_code == 200 and r.json()["result"] == SECRET_RESULT
    assert client.get("/jobs/job-99999", headers=headers).status_code == 404
    r = client.get("/api/fleet", headers=headers)
    assert r.status_code == 200 and r.json()[0]["name"] == "demo-module" and r.json()[0]["port"] == 18999
    r = client.get("/status", headers=headers)
    assert r.status_code == 200 and r.json()[0]["name"] == "demo-module"
    r = client.get("/notify/status", headers=headers)
    assert r.status_code == 200 and r.json()["notifier_enabled"] is True and r.json()["channels"] == [SECRET_CHANNEL]


@pytest.mark.parametrize("method", ["head", "options", "post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ROUTES)
def test_other_methods_never_return_content(world, path, method):
    client, fleet, probes, mp = world
    _configure(mp)
    r = getattr(client, method)(path)
    assert r.status_code in (401, 405), (method, path, r.status_code)
    _no_content(r)
    _untouched(fleet, probes)


@pytest.mark.parametrize("path", ["/jobs/job-00001/", "/jobs/job-00001%2F", "/jobs//job-00001", "/jobs/JOB-00001", "/jobs/job-00001;x=1", "/jobs/job-00001%00",
                                  "/api/fleet/", "/api/fleet%2F", "/api/Fleet", "/status/", "/Status", "/notify/status/", "/notify/Status", "/notify//status"])
def test_path_variants_do_not_bypass_the_credential(world, path):
    client, fleet, probes, mp = world
    _configure(mp)
    r = client.get(path)
    if r.status_code in (301, 302, 307, 308):                                # a redirect to the canonical route carries no content
        _no_content(r)
        assert client.get(r.headers["location"]).status_code in (401, 404, 405)
    else:
        assert r.status_code in (401, 404, 405), (path, r.status_code)
        _no_content(r)
    _untouched(fleet, probes)


@pytest.mark.parametrize("path", ["/health", "/modules", "/overview", "/permissions"])
def test_the_liveness_and_static_routes_stay_open(world, path):
    client, _, _, mp = world
    _configure(mp)
    assert client.get(path).status_code == 200
