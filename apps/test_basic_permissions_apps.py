"""Basic (AGPL) permissions at WHERE EXECUTION HAPPENS, for the ten apps that execute tools through their routes and operators, with the enterprise
package NOT installed (the import is blocked).

For every app: restricted start with no grants (every execution route and every capability denied, nothing executed), read-only grants (read
tools run, write tools denied), wrong principal, write grants (authorisation passes; approval stays separate), unclassified tools, and the
direct-route / ``/agent/run`` / operator paths. "Nothing executed" is proven by a network sentinel: a denied call makes no outbound connection;
the cores of these apps talk to their backends over the network, so any attempt shows up.
"""
from __future__ import annotations

import importlib
import json
import pathlib
import socket
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.permissions_setup import init_local_profile  # noqa: E402
from agentic_os.tool_actions import TOOL_ACTIONS  # noqa: E402

from _route_table import G, P, ROUTES as _ALL_ROUTES, START  # noqa: E402,F401
# the operator-authenticated route (`route-or-auth`) answers 401 before it reaches the gate: the route walk covers it
ROUTES = {a: [r for r in v if r[4] != "route-or-auth"] for a, v in _ALL_ROUTES.items()}
# reported, not changed: compliance.scan is registered non-side-effecting but runs the oscap script (a process launch that rewrites the results file): classified write
KNOWN_FLAG_DISAGREEMENTS = {("compliance", "compliance.scan"),
                            # FR-04: `terraform plan -out=redevops-approved.tfplan` overwrites the plan file infra.provision applies, with caller-supplied vars, and starts a
                            # provider-running process; infra.drift launches terraform / ansible with caller inputs: write by their actual effect (registered non-side-effecting)
                            ("infra", "infra.plan"), ("infra", "infra.drift")}
APPS = tuple(ROUTES)
assert len(APPS) == 17


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "AGENTIC_OS_PERMISSIONS", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE",
              "PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "PERMISSIONS_ALLOW_PLAINTEXT_DEV"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("SENTINEL_RATE_BURST", "1000000")                  # edge-sentinel's per-IP rate limiter answers 429 before the handler; not what is tested here
    monkeypatch.setenv("SENTINEL_RATE_PER_MIN", "1000000000")
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_PROCESS_AUTHORIZER", None)
    stub.absent(monkeypatch)                                  # the enterprise package is NOT installed (import blocked)


@pytest.fixture()
def net(monkeypatch):
    """The network sentinel: record every outbound attempt and refuse it (the cores talk to their backends over the network)."""
    attempts = []

    def refuse_connect(self, address, *a, **k):
        attempts.append(address)
        raise OSError("network blocked by the test")

    def refuse_lookup(host, *a, **k):
        attempts.append(host)
        raise socket.gaierror("network blocked by the test")
    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    monkeypatch.setattr(socket, "getaddrinfo", refuse_lookup)

    # processes too: a granted tool body may launch one (compliance.scan runs scan.sh, edge-sentinel shells out to docker/cscli, infra runs terraform): record the attempt
    # and refuse it, so no test ever executes a real scanner or writes into the repository
    import subprocess

    def refuse_process(*a, **k):
        attempts.append(("process", a[0] if a else k.get("args")))
        raise FileNotFoundError("process launch blocked by the test")
    for name in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(subprocess, name, refuse_process)
    monkeypatch.setattr("os.system", refuse_process)
    return attempts


def _start(monkeypatch, app):
    for name in [n for n in sys.modules if n == f"{app}.app"]:
        monkeypatch.delitem(sys.modules, name)
    return importlib.import_module(f"{app}.app")


def _operator(app):
    m = importlib.import_module(f"{app}.operator")
    return getattr(m, next(n for n in dir(m) if n.startswith("build_") and n.endswith("operator")))()


def _call(client, method, path, body):
    return client.request(method, path, json=body) if body is not None else client.request(method, path)


def _denied(resp, kind):
    """Did the app refuse this call under basic permissions (and say so in the documented shape)?"""
    if kind == "route":
        j = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        return resp.status_code == 403 and j.get("executed") is False and "[blocked]" in str(j.get("error"))
    j = resp.json()
    if kind == "console":
        return resp.status_code == 200 and j.get("denied") is True and "[blocked]" in j.get("text", "")
    return resp.status_code == 200 and j.get("executed") is False and "[blocked]" in str(j.get("say"))        # console-ok-false


def _reached(op, cap):
    """Did the capability get past the gate (its handler ran or failed on its own)? A denial is the documented dict."""
    try:
        out = op.invoke(cap, {})
    except Exception:  # noqa: BLE001 — the handler itself ran and failed: it was authorised
        return True
    return not (isinstance(out, dict) and out.get("status") == "denied" and "[blocked]" in str(out.get("error")))


def _routes(app, action=None):
    return [r for r in ROUTES[app] if action is None or TOOL_ACTIONS[app][r[3]] == action]


@pytest.mark.parametrize("app", APPS)
class TestEveryApp:
    def test_it_starts_restricted_with_nothing_provisioned_and_denies_every_execution_route(self, monkeypatch, app, net):
        mod = _start(monkeypatch, app)
        client = TestClient(mod.app, raise_server_exceptions=False)
        assert app_gate.gate_for(app).restricted is True
        for method, path, body, tool, kind in ROUTES[app]:
            r = _call(client, method, path, body)
            assert _denied(r, kind), (method, path, tool, r.status_code, r.text[:200])
            if kind != "console":
                assert "agentic-os permissions init" in r.text, (method, path)            # actionable
        assert net == []                                                                    # nothing executed

    def test_every_operator_capability_is_denied_and_nothing_executes(self, monkeypatch, app, net):
        _start(monkeypatch, app)
        op = _operator(app)
        assert op.manifest.capabilities
        for cap in op.manifest.capabilities:
            out = op.invoke(cap.name, {"tenant": "t", "doc_id": "d", "cloud": "aws"})
            assert out["status"] == "denied" and out["executed"] is False and "[blocked]" in out["error"], cap.name
        assert net == []

    def test_every_capability_and_route_tool_is_classified_and_matches_what_it_is_registered_as(self, monkeypatch, app):
        """Evidence cross-check: the trusted table classifies every capability, and read <=> not side_effecting."""
        table = TOOL_ACTIONS[app]
        _start(monkeypatch, app)
        for cap in _operator(app).manifest.capabilities:
            assert cap.name in table, f"unclassified capability {cap.name} (it would be denied under basic permissions)"
            if (app, cap.name) in KNOWN_FLAG_DISAGREEMENTS:
                continue
            assert (table[cap.name] != "read") == bool(cap.side_effecting), f"{cap.name}: table says {table[cap.name]}, registered side_effecting={cap.side_effecting}"
        for _, _, _, tool, _ in ROUTES[app]:
            assert tool in table, tool

    def test_a_read_grant_runs_read_tools_and_still_denies_write_tools(self, monkeypatch, app, tmp_path, net):
        init_local_profile(str(tmp_path / "perm"))                                         # read grants only
        mod = _start(monkeypatch, app)
        client = TestClient(mod.app, raise_server_exceptions=False)
        for method, path, body, tool, kind in ROUTES[app]:
            denied = _denied(_call(client, method, path, body), kind)
            assert denied == (TOOL_ACTIONS[app][tool] != "read"), (method, path, tool)
        op = _operator(app)
        for cap in op.manifest.capabilities:
            assert _reached(op, cap.name) == (TOOL_ACTIONS[app][cap.name] == "read"), cap.name

    def test_a_write_grant_authorises_the_write_tools_too(self, monkeypatch, app, tmp_path, net):
        init_local_profile(str(tmp_path / "perm"), include_write=True)
        mod = _start(monkeypatch, app)
        client = TestClient(mod.app, raise_server_exceptions=False)
        for method, path, body, tool, kind in ROUTES[app]:
            assert not _denied(_call(client, method, path, body), kind), (method, path, tool)
        op = _operator(app)
        assert all(_reached(op, cap.name) for cap in op.manifest.capabilities)

    def test_a_grant_that_belongs_to_another_user_authorises_nothing(self, monkeypatch, app, tmp_path, net):
        init_local_profile(str(tmp_path / "perm"), user="alice", include_write=True)
        (tmp_path / "perm" / "profile.json").write_text(json.dumps({"version": 1, "user": "mallory"}))      # the principal is not the grantee
        mod = _start(monkeypatch, app)
        client = TestClient(mod.app, raise_server_exceptions=False)
        for method, path, body, tool, kind in ROUTES[app]:
            assert _denied(_call(client, method, path, body), kind), (method, path, tool)
        assert net == []

    def test_request_headers_cannot_choose_the_principal_or_gain_an_admin_bypass(self, monkeypatch, app, tmp_path, net):
        init_local_profile(str(tmp_path / "perm"), user="alice")                           # read only
        mod = _start(monkeypatch, app)
        client = TestClient(mod.app, raise_server_exceptions=False)
        hdr = {"x-user": "alice", "x-roles": "admin,policy-admin", "authorization": "Bearer admin", "x-test-user": "granted"}
        for method, path, body, tool, kind in _routes(app, "write"):
            r = client.request(method, path, json=body, headers=hdr) if body is not None else client.request(method, path, headers=hdr)
            assert _denied(r, kind), (method, path, tool)

    def test_an_unclassified_tool_is_denied_even_with_every_grant(self, monkeypatch, app, tmp_path):
        init_local_profile(str(tmp_path / "perm"), include_write=True)
        _start(monkeypatch, app)
        assert app_gate.tool_denial(app, "unlisted.tool") is not None
        assert app_gate.deny_response(app, "unlisted.tool").status_code == 403
        assert app_gate.operator_denial(app, "unlisted.cap") is not None

    def test_the_gate_registry_holds_exactly_this_app(self, monkeypatch, app):
        _start(monkeypatch, app)
        assert set(app_gate._GATES) == {app} and app_gate.gate_for(app).table == TOOL_ACTIONS[app]


# ───────────── authorisation and approval stay separate ─────────────

APPROVAL_CAPABILITIES = {"growth-assistant": "assistant.push_assets", "infra": "infra.provision", "outreach-engine": "outreach.send_all",
                         "social-autopilot": "social.publish", "agentic-crm": "crm.qualify", "support": "support.resolve"}


@pytest.mark.parametrize("app,cap", sorted(APPROVAL_CAPABILITIES.items()))
def test_a_write_grant_does_not_replace_the_approval_the_capability_still_stages(monkeypatch, app, cap, tmp_path, net):
    init_local_profile(str(tmp_path / "perm"), include_write=True)
    _start(monkeypatch, app)
    op = _operator(app)
    spec = next(c for c in op.manifest.capabilities if c.name == cap)
    assert spec.approval_required and TOOL_ACTIONS[app][cap] == "write"
    out = op.invoke(cap, {"account": "x", "asset_id": "x", "cloud": "aws", "id": "1", "conversation_id": "1", "lead": "x"})
    # staged for approval (or refused by the core's own configuration check): in no case executed
    assert out.get("status") in ("pending_approval", "config_error") and not out.get("executed") and not out.get("publish_executed"), out


def test_without_the_write_grant_even_the_staging_of_an_approval_capability_is_denied(monkeypatch, tmp_path, net):
    init_local_profile(str(tmp_path / "perm"))
    _start(monkeypatch, "growth-assistant")
    out = _operator("growth-assistant").invoke("assistant.push_assets", {"asset_id": "x"})
    assert out["status"] == "denied"


def test_a_push_needs_the_push_grant_as_well_as_the_action_grant(monkeypatch, tmp_path, net):
    init_local_profile(str(tmp_path / "perm"), include_write=True)
    from agentic_os.permissions import GrantStore
    store = GrantStore(str(tmp_path / "perm"))
    for g in store.list():
        if g.resource_name.endswith("assistant.push_assets"):
            store.remove(g.id)
    mod = _start(monkeypatch, "growth-assistant")
    client = TestClient(mod.app, raise_server_exceptions=False)
    plain = client.post("/agent/run", json={"action": "playbook", "startup": START})
    pushed = client.post("/agent/run", json={"action": "playbook", "startup": START, "push": True})
    assert plain.status_code != 403 and pushed.status_code == 403 and "assistant.push_assets" in pushed.text


# ───────────── the operator's HTTP /invoke path end to end ─────────────

def test_the_http_invoke_route_denies_without_a_grant_and_runs_the_gate_before_the_handler(monkeypatch, tmp_path, net):
    mod = _start(monkeypatch, "growth-assistant")
    client = TestClient(mod.app, raise_server_exceptions=False)
    r = client.post("/invoke", json={"capability": "assistant.playbook", "inputs": {"startup": START}})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert r.json()["result"]["status"] == "denied" and net == []
    init_local_profile(str(tmp_path / "perm"), include_write=True)
    mod = _start(monkeypatch, "growth-assistant")
    client = TestClient(mod.app, raise_server_exceptions=False)
    r = client.post("/invoke", json={"capability": "assistant.playbook", "inputs": {"startup": START}})
    assert "[blocked]" not in r.text            # authorised: the handler ran (and failed on the blocked network on its own, a 500 or an error dict)


# ───────────── the consoles read the same authorizer; the enterprise stand-in present changes nothing for basic mode ─────────────

def test_the_books_console_is_gated_by_the_basic_authorizer_too(monkeypatch, tmp_path, net):
    mod = _start(monkeypatch, "books")
    client = TestClient(mod.app, raise_server_exceptions=False)
    assert client.post("api/agent", json={"message": "close status"}).json().get("denied") is True
    init_local_profile(str(tmp_path / "perm"))
    mod = _start(monkeypatch, "books")
    client = TestClient(mod.app, raise_server_exceptions=False)
    assert client.post("api/agent", json={"message": "close status"}).json().get("denied") is not True


@pytest.mark.parametrize("app", ["growth-assistant", "support"])
def test_with_a_stand_in_for_the_enterprise_package_present_basic_mode_behaves_the_same(monkeypatch, app, tmp_path, net):
    stub.install(monkeypatch, fleet={})
    init_local_profile(str(tmp_path / "perm"))
    mod = _start(monkeypatch, app)
    client = TestClient(mod.app, raise_server_exceptions=False)
    for method, path, body, tool, kind in ROUTES[app]:
        assert _denied(_call(client, method, path, body), kind) == (TOOL_ACTIONS[app][tool] != "read"), (method, path, tool)
