"""The local MCP bridge (integrations/mcp-bridge), driven as an MCP client drives it, against the REAL mounted gateway.

The Projects API is started on a real socket with the Governed Agent Gateway mounted (AGENT_GATEWAY_TOKEN); the bridge is
a Node process on stdio. Nothing is stubbed between them, so a route that answers 403 over a socket, or a tool list the
gateway does not grant, fails here. Skipped when Node is not installed.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "integrations" / "mcp-bridge" / "server" / "index.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="needs Node (Claude Desktop ships its own)")
TOKEN = "test-agent-token-0123456789abcdef"


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def deployment():
    port = _port()
    env = {**os.environ, "AGENT_GATEWAY_TOKEN": TOKEN, "PYTHONPATH": str(ROOT)}
    env.pop("AGENTIC_OS_PROFILE", None)
    code = ("import uvicorn; from agentic_os.projects_api import create_app, SampleProjectionProvider; "
            f"uvicorn.run(create_app(SampleProjectionProvider()), host='127.0.0.1', port={port}, log_level='warning')")
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                req = urllib.request.Request(url + "/api/agent-gateway/capabilities", headers={"Authorization": "Bearer " + TOKEN})
                with urllib.request.urlopen(req, timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:  # noqa: BLE001
                if proc.poll() is not None:
                    pytest.fail("the Projects API did not start: " + (proc.stdout.read().decode()[-400:] if proc.stdout else ""))
                time.sleep(0.2)
        else:
            pytest.fail("the gateway did not answer")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def bridge(messages, url, token=TOKEN, extra_env=None):
    env = {"PATH": os.environ.get("PATH", ""), "RAILS_URL": url, "RAILS_TOKEN": token, **(extra_env or {})}
    out = subprocess.run([NODE, str(BRIDGE)], input="".join(json.dumps(m) + "\n" for m in messages), capture_output=True, text=True, timeout=60, env=env)
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()], out.stderr


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
READY = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def test_a_standard_client_handshake_lists_what_the_gateway_grants_and_calls_through_the_governed_path(deployment):
    replies, _ = bridge([INIT, READY, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                         {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "projects_list", "arguments": {}}}], deployment)
    assert [r["id"] for r in replies] == [1, 2, 3]                                # the notification gets no reply
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18" and replies[0]["result"]["serverInfo"]["name"] == "rails-gateway-bridge"
    tools = {t["name"]: t for t in replies[1]["result"]["tools"]}
    granted = json.loads(urllib.request.urlopen(urllib.request.Request(deployment + "/api/agent-gateway/capabilities", headers={"Authorization": "Bearer " + TOKEN})).read())
    assert sorted(tools) == sorted(c["name"].replace(".", "_") for c in granted["capabilities"])      # exactly the gateway's list, nothing added
    assert all(t["inputSchema"]["type"] == "object" for t in tools.values()) and "[rAIls capability projects.list]" in tools["projects_list"]["description"]
    call = replies[2]["result"]
    assert call["isError"] is False and json.loads(call["content"][0]["text"])["status"] == "ok" and call["structuredContent"]["request_id"]


def test_a_wrong_token_gets_no_tools_and_no_data(deployment):
    replies, err = bridge([INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                           {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "projects_list", "arguments": {}}}], deployment, token="wrong-token")
    assert "refused this token" in replies[1]["error"]["message"]
    assert replies[2]["result"]["isError"] is True and "refused this token" in replies[2]["result"]["content"][0]["text"]
    assert "wrong-token" not in err and TOKEN not in err


def test_a_capability_the_token_is_not_offered_cannot_be_called_by_guessing_its_name(deployment):
    replies, _ = bridge([INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "sandbox_execute", "arguments": {"cmd": "id"}}},
                         {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "projects.list/../../admin", "arguments": {}}}], deployment)
    for r in replies[1:]:
        assert r["result"]["isError"] is True and "not offered" in r["result"]["content"][0]["text"]


def test_the_token_is_never_sent_in_cleartext_to_another_machine_and_a_missing_setting_is_said_plainly():
    for url, needle in (("http://example.com", "cleartext"), ("https://user:pw@example.com", "must not carry credentials"), ("", "RAILS_URL is not set"), ("not a url", "not a URL")):
        replies, err = bridge([INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                               {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "x", "arguments": {}}}], url)
        assert replies[0]["result"]["serverInfo"]                                  # the handshake still completes, so the client can show the reason
        assert needle in replies[1]["error"]["message"] and needle in replies[2]["result"]["content"][0]["text"] and TOKEN not in err
    replies, _ = bridge([INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}], "http://127.0.0.1:9", token="")
    assert "RAILS_TOKEN is not set" in replies[1]["error"]["message"]


def test_the_extension_manifest_is_a_valid_mcpb_manifest_that_keeps_the_token_in_secure_storage():
    m = json.loads((ROOT / "integrations" / "mcp-bridge" / "manifest.json").read_text(encoding="utf-8"))
    assert all(m.get(k) for k in ("manifest_version", "name", "version", "description", "author", "server"))
    assert m["server"]["type"] == "node" and (ROOT / "integrations" / "mcp-bridge" / m["server"]["entry_point"]).is_file()
    assert m["server"]["mcp_config"]["args"] == ["${__dirname}/server/index.js"]
    assert m["user_config"]["rails_token"]["sensitive"] is True and m["user_config"]["rails_token"]["required"] is True
    assert m["server"]["mcp_config"]["env"] == {"RAILS_URL": "${user_config.rails_url}", "RAILS_TOKEN": "${user_config.rails_token}"}
    src = BRIDGE.read_text(encoding="utf-8")
    assert "require('node:" in src and not [line for line in src.splitlines() if "require(" in line and "node:" not in line and "require.main" not in line]   # standard library only


def test_with_an_agents_directory_and_a_token_file_the_bridge_finds_the_running_app_by_itself(deployment, tmp_path):
    """No address and no token in the client's settings: the app publishes where it listens, the token is read from its private file."""
    agents = tmp_path / "agents"
    (agents / "tokens").mkdir(parents=True)
    token_file = agents / "tokens" / "agent-1.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    port = deployment.rsplit(":", 1)[1]
    gw = agents / "gateway.json"
    env = {"RAILS_AGENTS_DIR": str(agents), "RAILS_TOKEN_FILE": str(token_file)}
    ask = [INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]

    def run():
        out = subprocess.run([NODE, str(BRIDGE)], input="".join(json.dumps(m) + "\n" for m in ask), capture_output=True, text=True, timeout=60, env={"PATH": os.environ.get("PATH", ""), **env})
        return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]

    assert "not running" in run()[1]["error"]["message"]                                   # nothing published: say so, offer nothing
    gw.write_text(json.dumps({"schema": "agentic-os.local-gateway/1", "url": f"http://127.0.0.1:{port}", "pid": os.getpid()}), encoding="utf-8")
    assert "projects_list" in [t["name"] for t in run()[1]["result"]["tools"]]
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    gw.write_text(json.dumps({"schema": "agentic-os.local-gateway/1", "url": f"http://127.0.0.1:{port}", "pid": dead.pid}), encoding="utf-8")
    assert "not running" in run()[1]["error"]["message"]                                   # a stale file is not followed
    gw.write_text(json.dumps({"schema": "agentic-os.local-gateway/1", "url": "http://evil.example:80", "pid": os.getpid()}), encoding="utf-8")
    assert "not written by the rAIls app" in run()[1]["error"]["message"]                  # only a loopback address is followed
    gw.write_text(json.dumps({"schema": "agentic-os.local-gateway/1", "url": f"http://127.0.0.1:{port}", "pid": os.getpid()}), encoding="utf-8")
    token_file.unlink()
    assert "not connected to the rAIls app any more" in run()[1]["error"]["message"]       # the credential was removed
