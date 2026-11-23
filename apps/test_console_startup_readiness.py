"""The console apps at boot, with the whole stack installed (agent-harness C1/C2 + context-runtime C3 + C4 here).

Four apps hang an assistant off a Context Runtime ``AgentConsole``: growth-engine, market-radar, agentic-privacy
and edge-sentinel. They are where the three packages actually meet, and where the two failure modes this stack
produces show up: an app that serves an assistant it cannot authorize (every action answered
``missing_explicit_grant``, which a user reads as "the permissions are broken"), and an app whose gate answers
"allowed" in a dialect the installed engine reads as silence.

So: the console is checked before the app can serve; ``/health`` says what is true rather than what is hoped; and
an authorized read on a provisioned instance actually reaches the tool.
"""
from __future__ import annotations

import importlib
import pathlib
import re
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tests"))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.permissions_setup import init_local_profile  # noqa: E402

agent_console = pytest.importorskip("context_runtime.integrations.agent_console")

CONSOLE_APPS = ("growth-engine", "market-radar", "agentic-privacy", "edge-sentinel")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for key in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "AGENTIC_OS_PERMISSIONS", "PERMISSIONS_KEY",
                "PERMISSIONS_KEY_FILE", "PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "PERMISSIONS_ALLOW_PLAINTEXT_DEV",
                app_gate.CONSOLE_ACK, "CR_LOCAL_AUTHORITY", "UMAMI_BASE_URL", "UMAMI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_PROCESS_AUTHORIZER", None)
    stub.absent(monkeypatch)                      # the enterprise package is NOT installed
    from context_runtime.tools.base import set_default_authorizer
    set_default_authorizer(None)                  # the engine's default is process-wide: start each app clean
    yield
    set_default_authorizer(None)


def _boot(monkeypatch, app: str, *, block_helper_servers: bool = True):
    """Import an app the way a restart does, with nothing cached from the previous test."""
    for name in [n for n in sys.modules if n == f"{app}.app"]:
        monkeypatch.delitem(sys.modules, name)
    if block_helper_servers:                      # market-radar mounts an MCP server: an optional extra, not the point
        monkeypatch.setattr(subprocess, "Popen",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("helper servers blocked by the test")))
    return importlib.import_module(f"{app}.app")


@pytest.mark.parametrize("app", CONSOLE_APPS)
class TestTheConsoleIsCheckedBeforeItServes:
    def test_a_provisioned_app_boots_with_a_ready_console_and_says_so(self, monkeypatch, app, tmp_path):
        init_local_profile(str(tmp_path / "perm"))                       # the documented quickstart: read grants
        module = _boot(monkeypatch, app)
        state = app_gate.console_state(app)
        assert state is not None and state["ready"] is True, state
        assert state["authorizer"] in ("registry", "process_default"), state
        health = TestClient(module.app, raise_server_exceptions=False).get("/health").json()
        assert health["authorization"] == "enforced"
        assert health["authorization_ready"] is True                     # alive AND able to authorize what it offers

    def test_the_gate_is_installed_before_the_console_is_built(self, app):
        """The ordering is what makes the start-up check meaningful: the console must not be built first and inherit
        no plane, or a gated app would validate an assistant that can never act."""
        source = (HERE / app / "app.py").read_text(encoding="utf-8")
        installed = source.index("_install_gate(_GATE_APP)")
        # the assignment, not the import: each app builds its console once, under its own name
        built = re.search(r"^\s*_?[A-Z_]*CONSOLE = ", source, re.M).start()
        assert installed < built, f"{app}: the console is built before the gate is installed"


class TestTheAuthorizedPathReachesTheTool:
    """The end-to-end half of the contract: a grant, on a running app, does what a user expects it to do."""

    def test_a_read_grant_lets_the_assistant_run_its_read_tool(self, monkeypatch, tmp_path):
        init_local_profile(str(tmp_path / "perm"))                      # read grants only
        module = _boot(monkeypatch, "growth-engine")
        client = TestClient(module.app, raise_server_exceptions=False)
        answer = client.post("/api/agent", json={"message": "traffic summary"}).json()
        assert answer.get("intent") == "action", answer
        assert "[blocked]" not in answer.get("text", ""), answer
        assert answer.get("approved") is True, answer                    # authorized AND allowed by the approval policy

    def test_the_same_call_on_an_unprovisioned_instance_is_denied_with_the_fix(self, monkeypatch, tmp_path):
        module = _boot(monkeypatch, "growth-engine")                     # requested, nothing provisioned: RESTRICTED
        client = TestClient(module.app, raise_server_exceptions=False)
        assert app_gate.gate_for("growth-engine").restricted is True
        answer = client.post("/api/agent", json={"message": "traffic summary"}).json()
        assert "[blocked]" in answer.get("text", ""), answer
        assert "permissions init" in answer.get("text", ""), answer       # actionable, not a shrug
        assert client.get("/health").json()["authorization"] == "restricted"

    def test_a_write_tool_stays_denied_under_read_grants(self, monkeypatch, tmp_path):
        init_local_profile(str(tmp_path / "perm"))
        module = _boot(monkeypatch, "market-radar")
        client = TestClient(module.app, raise_server_exceptions=False)
        denied = client.post("/agent/run", json={"action": "add_watch", "url": "https://example.com"}).json()
        assert denied.get("executed") is False, denied                    # the watch was not added
        refusal = f"{denied.get('reason', '')} {denied.get('error', '')}".lower()
        assert "grant" in refusal, denied        # it names the missing grant, not just "no"
        allowed = client.get("/api/activity").json()
        assert not (isinstance(allowed, dict) and allowed.get("executed") is False)   # the read route is not denied


class TestTheDiscoveredServerWaitsForProvisioning:
    def test_a_restricted_market_radar_starts_no_mcp_server_and_registers_no_discovered_tool(self, monkeypatch, tmp_path):
        """Discovery means running something. A restricted app runs nothing, so the discovered tool is not offered."""
        spawned: list = []
        for name in [n for n in sys.modules if n == "market-radar.app"]:
            monkeypatch.delitem(sys.modules, name)

        def record(*a, **k):
            spawned.append(a[0] if a else k.get("args"))
            raise OSError("blocked")
        monkeypatch.setattr(subprocess, "Popen", record)
        module = importlib.import_module("market-radar.app")
        console = module._RADAR_CONSOLE
        assert console is not None and "web_search" not in console._tools
        assert not spawned, f"a RESTRICTED app started a helper process anyway: {spawned}"
        assert app_gate.gate_for("market-radar").restricted is True

    def test_a_provisioned_market_radar_mounts_it_with_the_class_from_the_table(self, monkeypatch, tmp_path):
        init_local_profile(str(tmp_path / "perm"))
        module = _boot(monkeypatch, "market-radar", block_helper_servers=False)
        spec = module._RADAR_CONSOLE._tools["web_search"].spec()
        assert spec.action == "read", "the class must come from agentic_os/tool_actions.py, not the server's hint"
        assert spec.side_effecting is False                              # the readOnlyHint is the approval axis

    def test_the_mounted_tool_still_needs_its_grant_for_the_requesting_principal(self, monkeypatch, tmp_path):
        """Registered ≠ authorized: a read-class tool for a principal whose grants are elsewhere is refused."""
        from agentic_os.permissions import GrantStore, make_authorizer, make_engine_authorizer
        init_local_profile(str(tmp_path / "perm"))
        module = _boot(monkeypatch, "market-radar", block_helper_servers=False)
        stranger = make_engine_authorizer(make_authorizer(
            GrantStore(_other_tenant_dir(tmp_path)),
            tool_actions={"web_search": "read"}, tool_resources={"web_search": ("tool", "market-radar.web_search")}))
        assert module._RADAR_CONSOLE._tools["web_search"].spec().action == "read"
        assert stranger(_Nobody(), module._RADAR_CONSOLE._tools["web_search"].spec(), {}) is not None


def _other_tenant_dir(tmp_path) -> str:
    """A second, separately provisioned permissions directory: the profile THIS app does not belong to."""
    from agentic_os.permissions import provision_key_file
    directory = str(tmp_path / "other")
    provision_key_file(directory)
    return directory


class _Nobody:
    app, user, roles = "", "", []