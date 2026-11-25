"""An external agent on a locally installed app: scoped credentials, discovery, and what such a credential cannot do.

The sidecar is started the way the launcher starts it (packaged, production profile, per-user state in a temporary home)
and everything is asked over the real socket.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentic_os import desktop_profile
from agentic_os.local_agents import (GRANTABLE, LocalAgentError, LocalAgentStore, connect_agent, disconnect_agent, discover_gateway, publish_gateway,
                                     unpublish_gateway)

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "packaging" / "sidecar_main.py"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def call(url, *, token=None, method="GET", body=None, headers=None):
    h = {"content-type": "application/json", **(headers or {})}
    if token:
        h["authorization"] = "Bearer " + token
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"raw": raw[:200].decode(errors="replace")}


# ── the store ────────────────────────────────────────────────────────────────────────────────────────────

def test_no_agent_exists_until_one_is_connected_and_a_credential_is_a_private_file_not_a_value(tmp_path):
    store = LocalAgentStore(tmp_path / "agents")
    assert store.list() == [] and store.verify("rla_anything") is None
    rec, path = store.connect("Gemini CLI", grants=["projects.read"], client="gemini-cli")
    token = path.read_text().strip()
    assert token.startswith("rla_") and token not in (tmp_path / "agents" / "agents.json").read_text()      # only the hash is stored
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o600" and oct((tmp_path / "agents" / "agents.json").stat().st_mode & 0o777) == "0o600"
    assert store.verify(token).id == rec.id and store.grants_of(rec.id) == ("projects.read",)
    assert "token_sha256" not in rec.public() and rec.public()["active"] is True


def test_there_is_no_grant_for_everything_and_an_unknown_grant_is_refused(tmp_path):
    store = LocalAgentStore(tmp_path / "agents")
    for bad in (["*"], ["admin"], ["projects.write"], ["projects.read", "sandbox.execute"], []):
        with pytest.raises(LocalAgentError):
            store.connect("x", grants=bad)
    with pytest.raises(LocalAgentError):
        store.connect("   ")
    assert store.list() == [] and set(GRANTABLE) == {"projects.read", "missions.read", "sources.read", "dashboards.read"}


def test_a_revoked_agent_is_nobody_and_an_unreadable_store_admits_nobody(tmp_path):
    store = LocalAgentStore(tmp_path / "agents")
    rec, path = store.connect("Local Qwen agent")
    token = path.read_text().strip()
    assert store.revoke(rec.id) is True and store.verify(token) is None and store.grants_of(rec.id) == () and not path.exists()
    assert store.revoke(rec.id) is False
    rec2, path2 = store.connect("second")
    token2 = path2.read_text().strip()
    (tmp_path / "agents" / "agents.json").write_text("{ not json")
    assert store.verify(token2) is None and store.grants_of(rec2.id) == ()
    with pytest.raises(LocalAgentError):
        store.list()


def test_discovery_never_returns_an_address_for_a_process_that_is_gone(tmp_path):
    assert discover_gateway(tmp_path)["running"] is False
    publish_gateway(tmp_path, port=8787, version="t")
    assert discover_gateway(tmp_path) == {"running": True, "url": "http://127.0.0.1:8787", "pid": os.getpid(), "version": "t"}
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    publish_gateway(tmp_path, port=8787, pid=dead.pid)
    assert discover_gateway(tmp_path)["running"] is False and "stale" in discover_gateway(tmp_path)["reason"]
    unpublish_gateway(tmp_path)                                    # not ours (another pid): left alone
    assert (tmp_path / "gateway.json").exists()
    (tmp_path / "gateway.json").write_text(json.dumps({"schema": "agentic-os.local-gateway/1", "url": "http://evil.example:80", "pid": os.getpid()}))
    assert discover_gateway(tmp_path)["running"] is False          # only a loopback address this app wrote


# ── the installed app, over the socket ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    port = _free_port()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home), "XDG_STATE_HOME": str(home / "state"),
           "LOCALAPPDATA": str(home / "state"), "PYTHONPATH": str(REPO), "PROJECTS_PORT": str(port), "PROJECTS_HOST": "127.0.0.1"}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    probe = dict(env, AGENTIC_OS_PACKAGED="1")
    desktop_profile.apply(probe, home=str(home))
    agents_dir = Path(probe["AGENTIC_OS_LOCAL_AGENTS_DIR"])
    log = home / "serve.log"
    with open(log, "wb") as fh:
        p = subprocess.Popen([sys.executable, str(ENTRY), "serve", "--exit-on-stdin-eof"], env=env, cwd=home, stdin=subprocess.PIPE, stdout=fh, stderr=subprocess.STDOUT)
    try:
        end = time.time() + 90
        while time.time() < end and p.poll() is None and not discover_gateway(agents_dir)["running"]:
            time.sleep(0.2)
        found = discover_gateway(agents_dir)
        assert found["running"], "the app did not publish its gateway:\n" + log.read_text(errors="replace")[-2500:]
        assert found["url"] == f"http://127.0.0.1:{port}"
        yield {"url": found["url"], "agents": LocalAgentStore(agents_dir), "dir": agents_dir, "proc": p, "log": log, "env": probe}
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()


def test_a_connected_agent_reaches_the_gateway_with_its_own_grants_and_nothing_else(app):
    out = connect_agent("Gemini CLI", grants=["projects.read"], client="gemini-cli", env=app["env"])
    assert out["gateway"]["url"] == app["url"] and out["expires_in_days"] == 30 and out["agent"]["grants"] == ["projects.read"]
    token = Path(out["token_file"]).read_text().strip()
    s, caps = call(app["url"] + "/api/agent-gateway/capabilities", token=token)
    names = sorted(c["name"] for c in caps["capabilities"])
    assert s == 200 and names and all(n.startswith("projects.") for n in names), (s, caps)        # only what projects.read shows
    s, out = call(app["url"] + f"/api/agent-gateway/capabilities/{names[0]}/invoke", token=token, method="POST", body={})
    assert s == 200 and out["status"] == "ok", (s, out)
    s, out = call(app["url"] + "/api/agent-gateway/capabilities/missions.list/invoke", token=token, method="POST", body={})
    assert s == 200 and out["status"] != "ok", "a capability outside the agent's grants ran"
    # the agent's token is not a session: every other route of the app refuses it
    for path_ in ("/api/projects", "/api/dashboards", "/api/local/session", "/api/ui-config"):
        assert call(app["url"] + path_, token=token)[0] in (401, 403), path_
    assert call(app["url"] + "/api/projects/main/capture", token=token, method="POST", body={"content": "x"})[0] in (401, 403)


def test_without_a_connected_agent_or_with_a_page_origin_the_gateway_admits_nobody(app):
    assert call(app["url"] + "/api/agent-gateway/capabilities")[0] == 401
    assert call(app["url"] + "/api/agent-gateway/capabilities", token="rla_" + "x" * 43)[0] == 401
    token = Path(connect_agent("page test", env=app["env"])["token_file"]).read_text().strip()
    assert call(app["url"] + "/api/agent-gateway/capabilities", token=token)[0] == 200
    for origin in ("https://evil.example", app["url"]):                    # a web page, even this app's own origin, is not an agent
        assert call(app["url"] + "/api/agent-gateway/capabilities", token=token, headers={"origin": origin})[0] in (401, 403)


def test_a_revocation_holds_from_the_next_request(app):
    out = connect_agent("to be revoked", env=app["env"])
    token = Path(out["token_file"]).read_text().strip()
    assert call(app["url"] + "/api/agent-gateway/capabilities", token=token)[0] == 200
    assert disconnect_agent(out["agent"]["id"], env=app["env"])
    assert call(app["url"] + "/api/agent-gateway/capabilities", token=token)[0] == 401
    assert call(app["url"] + "/api/agent-gateway/capabilities/projects.list/invoke", token=token, method="POST", body={})[0] == 401


def test_a_token_without_an_authority_envelope_is_admitted_to_the_gateway_and_then_denied_every_call(app):
    """The production profile runs the gateway fail-closed: a credential alone (no envelope, or an expired or revoked one) does nothing."""
    rec, path = app["agents"].connect("credential only")                 # the store alone: no authority was issued
    token = path.read_text().strip()
    s, out = call(app["url"] + "/api/agent-gateway/capabilities/projects.list/invoke", token=token, method="POST", body={})
    assert s == 200 and out["status"] == "denied", (s, out)
    ok = connect_agent("with authority", env=app["env"])
    tok2 = Path(ok["token_file"]).read_text().strip()
    assert call(app["url"] + "/api/agent-gateway/capabilities/projects.list/invoke", token=tok2, method="POST", body={})[1]["status"] == "ok"
    from agentic_os.local_agents import revoke_authority
    revoke_authority(ok["agent"]["id"], app["env"]["AGENTIC_OS_AUTHORITY_DB"])          # authority revoked, token still valid
    assert call(app["url"] + "/api/agent-gateway/capabilities/projects.list/invoke", token=tok2, method="POST", body={})[1]["status"] == "denied"
    for days in (0, -1, 91, 3650):
        with pytest.raises(LocalAgentError):
            connect_agent("too long", days=days, env=app["env"])


def test_the_token_prefix_the_gate_looks_for_is_the_one_the_store_issues():
    from agentic_os import local_agents, local_capability
    assert local_capability.LOCAL_AGENT_TOKEN_PREFIX == local_agents.TOKEN_PREFIX


# ── the whole path a user takes: the app's own command, then an MCP client through the shipped bridge ────

import shutil

NODE = shutil.which("node")


def _sidecar(args, env):
    return subprocess.run([sys.executable, str(ENTRY), *args], env=env, capture_output=True, text=True, timeout=120)


def test_the_agent_command_connects_lists_and_disconnects_and_never_prints_a_token(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home), "XDG_STATE_HOME": str(home / "state"),
           "LOCALAPPDATA": str(home / "state"), "PYTHONPATH": str(REPO)}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    settings = tmp_path / "project" / ".gemini" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}))
    r = _sidecar(["agent", "connect", "--name", "Gemini CLI", "--client", "gemini-cli", "--grants", "projects.read", "--write-settings", str(settings)], env)
    assert r.returncode == 0, r.stderr[-800:]
    out = json.loads(r.stdout)
    token = Path(out["token_file"]).read_text().strip()
    assert token not in r.stdout and token not in r.stderr and token not in settings.read_text()
    assert out["gateway"]["running"] is False and out["agent"]["grants"] == ["projects.read"] and out["expires_in_days"] == 30
    written = json.loads(settings.read_text())
    assert written["theme"] == "dark" and written["mcpServers"]["other"] == {"command": "x"}                     # the rest of the file is untouched
    server = written["mcpServers"]["rails"]
    assert server == out["mcp_server"] and server["command"] == "node" and Path(server["args"][0]).is_file()
    assert set(server["env"]) == {"RAILS_AGENTS_DIR", "RAILS_TOKEN_FILE"}
    listed = json.loads(_sidecar(["agent", "list"], env).stdout)["agents"]
    assert [a["name"] for a in listed] == ["Gemini CLI"] and "token_sha256" not in json.dumps(listed)
    assert json.loads(_sidecar(["agent", "disconnect", out["agent"]["id"]], env).stdout)["disconnected"] is True
    assert json.loads(_sidecar(["agent", "list"], env).stdout)["agents"][0]["active"] is False
    bad = _sidecar(["agent", "connect", "--name", "x", "--grants", "projects.read,everything"], env)
    assert bad.returncode == 1 and "not grantable" in bad.stdout
    assert _sidecar(["agents"], env).returncode == 2                                                            # an unknown command still starts nothing
    broken = tmp_path / "broken.json"
    broken.write_text("[1, 2")
    r = _sidecar(["agent", "connect", "--name", "y", "--write-settings", str(broken)], env)
    assert r.returncode == 1 and broken.read_text() == "[1, 2"                                                  # a file it cannot understand is not overwritten


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_an_mcp_client_reaches_the_running_app_through_the_shipped_bridge_with_two_paths_and_no_secret(app):
    out = connect_agent("MCP client", grants=["projects.read", "missions.read"], client="generic", env=app["env"])
    from agentic_os.local_agents import mcp_server_config
    cfg = mcp_server_config(out["token_file"], str(app["dir"]))
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "projects_list", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "sources_search", "arguments": {"query": "x"}}}]
    r = subprocess.run([NODE, *cfg["args"]], input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ.get("PATH", ""), **cfg["env"]})
    replies = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
    assert "result" in replies[1], (replies, r.stderr[-400:])                           # on failure, the bridge's own words
    names = sorted(t["name"] for t in replies[1]["result"]["tools"])
    assert names and all(n.startswith(("projects_", "missions_")) for n in names), names                         # its grants, nothing more
    assert replies[2]["result"]["isError"] is False and json.loads(replies[2]["result"]["content"][0]["text"])["status"] == "ok"
    assert replies[3]["result"]["isError"] is True                                                               # sources.read was not granted
    token = Path(out["token_file"]).read_text().strip()
    assert token not in json.dumps(cfg) and token not in r.stderr


def test_dashboards_are_readable_only_by_an_agent_that_was_granted_them_by_name(app):
    """dashboards.read is not a default grant. With it an agent reads the figures the app shows (a reported test run included);
    without it the capability is not even listed."""
    default = connect_agent("no dashboards", env=app["env"])
    t_default = Path(default["token_file"]).read_text().strip()
    s, caps = call(app["url"] + "/api/agent-gateway/capabilities", token=t_default)
    assert s == 200 and not [c for c in caps["capabilities"] if c["name"].startswith("dashboards.")]
    s, out = call(app["url"] + "/api/agent-gateway/capabilities/dashboards.list/invoke", token=t_default, method="POST", body={})
    assert out["status"] != "ok"
    granted = connect_agent("QA reader", grants=["dashboards.read"], env=app["env"])
    token = Path(granted["token_file"]).read_text().strip()
    s, caps = call(app["url"] + "/api/agent-gateway/capabilities", token=token)
    assert sorted(c["name"] for c in caps["capabilities"]) == ["dashboards.get", "dashboards.list"]        # nothing else: no projects, no missions
    out = call(app["url"] + "/api/agent-gateway/capabilities/dashboards.list/invoke", token=token, method="POST", body={})[1]
    assert out["status"] == "ok" and "dashboards" in out["output"]
    ids = [d["id"] for d in out["output"]["dashboards"]]
    assert "portfolio" in ids
    got = call(app["url"] + "/api/agent-gateway/capabilities/dashboards.get/invoke", token=token, method="POST", body={"dashboard_id": "portfolio", "days": 7})[1]
    assert got["status"] == "ok" and got["output"]["id"] == "portfolio" and got["output"]["panels"] and "drills" not in got["output"]
    bad = call(app["url"] + "/api/agent-gateway/capabilities/dashboards.get/invoke", token=token, method="POST", body={"dashboard_id": "nope"})[1]
    assert bad["status"] == "ok" and "error" in bad["output"]                       # an unknown dashboard is an answer, not a crash
    # the granted agent still cannot reach anything outside the gateway
    assert call(app["url"] + "/api/dashboards", token=token)[0] in (401, 403)


# last on purpose: it stops the app the tests above share
def test_the_gateway_file_is_removed_when_the_app_stops(app):
    assert (app["dir"] / "gateway.json").exists()
    app["proc"].stdin.close()
    app["proc"].wait(timeout=30)
    assert not (app["dir"] / "gateway.json").exists() and discover_gateway(app["dir"])["running"] is False
