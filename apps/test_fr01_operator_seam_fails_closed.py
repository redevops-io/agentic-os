"""FR-01: the Operator seam must FAIL CLOSED. With basic permissions requested, an operator whose name has no gate registered used to run every capability
ungated (the check did nothing without a gate): the shipped suite server and the app-less sky / video-ad / zoning. ``POST /m/compliance/invoke compliance.scan``
launched ``bash scan.sh`` with no key, no profile and no grant.

Now: no gate registered + permissions requested = DENIED with an actionable message; the suite server installs the gate of every app it serves; and a walk over EVERY
operator construction site fails if an operator can execute with permissions requested and no gate.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.mission.operator_sdk import Operator, capability  # noqa: E402
from agentic_os.permissions_setup import init_local_profile  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
APPS_DIR = pathlib.Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "AGENTIC_OS_PERMISSIONS", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE", "SUITE", "SUITE_BASE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_PROCESS_AUTHORIZER", None)
    stub.absent(monkeypatch)


def _recording_op(name="some-app"):
    ran = []
    return Operator(name, [capability("x.write", lambda i: ran.append(1) or {"ok": 1}, side_effecting=True),
                           capability("x.read", lambda i: ran.append(1) or {"ok": 1})]), ran


class TestTheSeamFailsClosedWithoutAGate:
    @pytest.mark.parametrize("value", ["1", "true", "on", "enabled"])
    def test_permissions_requested_and_no_gate_denies_every_capability_with_an_actionable_message(self, monkeypatch, value):
        monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", value)
        op, ran = _recording_op()
        for cap in ("x.write", "x.read"):
            out = op.invoke(cap, {})
            assert out["status"] == "denied" and out["executed"] is False and "[blocked]" in out["error"]
            assert "install_gate" in out["error"] and "some-app" in out["error"]
        assert ran == []

    def test_nothing_requested_changes_nothing(self):
        op, ran = _recording_op()
        assert op.invoke("x.write", {}) == {"ok": 1} and ran == [1]

    def test_an_installed_gate_still_decides_as_before(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
        init_local_profile(str(tmp_path / "perm"))
        app_gate.install_gate("growth-assistant")
        op = Operator("growth-assistant", [capability("assistant.ask", lambda i: {"ok": 1}), capability("assistant.playbook", lambda i: {"ok": 1}, side_effecting=True)])
        assert op.invoke("assistant.ask", {}) == {"ok": 1}
        assert op.invoke("assistant.playbook", {})["status"] == "denied"

    def test_the_name_of_a_kernel_plane_operator_exempts_nothing(self, monkeypatch):
        monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
        assert app_gate.KERNEL_GOVERNED_MODULES == {"agentic_os.interaction.operator", "agentic_os.agent_gateway.social.operator", "agentic_os.agent_gateway.external.operator"}
        op, ran = _recording_op("interaction")                               # DR-04: the NAME exempts nothing
        out = op.invoke("x.write", {})
        assert out["status"] == "denied" and ran == []

    def test_enterprise_mode_without_a_gate_for_the_operator_is_denied_too(self, monkeypatch):
        """Design decision: the execution boundary does not depend on an app having installed a gate, in either mode."""
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        stub.install(monkeypatch, fleet={})
        op, ran = _recording_op()
        out = op.invoke("x.read", {})
        assert out["status"] == "denied" and out["executed"] is False and "enterprise permissions are on" in out["error"] and ran == []

    def test_enterprise_mode_with_the_gate_installed_evaluates_the_requests_principal_and_in_process_calls_are_anonymous(self, monkeypatch):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        stub.install(monkeypatch, fleet={})
        app_gate.install_gate("growth-assistant")
        op = Operator("growth-assistant", [capability("assistant.ask", lambda i: {"ok": 1})])
        assert op.invoke("assistant.ask", {})["status"] == "denied"                    # no request: the anonymous principal (a service identity must be configured)
        from starlette.requests import Request
        tok = app_gate._REQUEST.set(Request({"type": "http", "headers": [(b"x-test-user", b"granted")], "method": "POST", "path": "/invoke", "query_string": b""}))
        try:
            assert op.invoke("assistant.ask", {}) == {"ok": 1}                         # the granted principal of THIS request
        finally:
            app_gate._REQUEST.reset(tok)


class TestSuiteServer:
    def _server(self, monkeypatch, suite):
        monkeypatch.setenv("SUITE", suite)
        monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
        monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
        path = APPS_DIR / "_suite-server" / "serve.py"
        spec = importlib.util.spec_from_file_location("suite_serve_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_it_installs_a_gate_for_every_app_it_serves(self, monkeypatch):
        mod = self._server(monkeypatch, "security-compliance")
        assert set(app_gate._GATES) == {"compliance", "agentic-privacy", "edge-sentinel"}
        assert mod._app is not None

    def test_the_demonstrated_exploit_compliance_scan_launches_nothing(self, monkeypatch):
        launched = []
        real_run, real_popen = subprocess.run, subprocess.Popen
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: launched.append(a) or real_run(["true"]))
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: launched.append(a) or real_popen(["true"]))
        mod = self._server(monkeypatch, "security-compliance")
        client = TestClient(mod._app, raise_server_exceptions=False)
        for cap, body in (("compliance.scan", {}), ("sentinel.block_ip", {"ip": "203.0.113.5"}), ("compliance.remediate", {})):
            op = "compliance" if cap.startswith("compliance") else "edge-sentinel"
            r = client.post(f"/m/{op}/invoke", json={"capability": cap, "inputs": body})
            assert r.status_code == 200 and r.json()["result"]["status"] == "denied", (cap, r.text[:200])
        assert launched == []

    def test_with_a_profile_the_suite_server_enforces_the_grants_too(self, monkeypatch, tmp_path):
        init_local_profile(str(tmp_path / "perm"))                           # read grants only
        mod = self._server(monkeypatch, "security-compliance")
        client = TestClient(mod._app, raise_server_exceptions=False)
        denied = client.post("/m/compliance/invoke", json={"capability": "compliance.scan", "inputs": {}}).json()["result"]
        assert denied["status"] == "denied"
        allowed = client.post("/m/compliance/invoke", json={"capability": "compliance.explain", "inputs": {}})
        assert "[blocked]" not in allowed.text


class TestEveryOperatorSite:
    def test_every_app_operator_denies_every_capability_when_permissions_are_requested_and_no_gate_is_installed(self, monkeypatch):
        monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
        sys.path.insert(0, str(APPS_DIR))
        checked = {}
        for op_file in sorted(APPS_DIR.glob("*/operator.py")):
            app = op_file.parent.name
            try:
                m = importlib.import_module(f"{app}.operator")
            except Exception as e:  # noqa: BLE001 — an operator that cannot even be built here cannot execute
                checked[app] = f"not built ({type(e).__name__})"
                continue
            op = getattr(m, next(n for n in dir(m) if n.startswith("build_") and n.endswith("operator")))()
            for cap in op.manifest.capabilities:
                out = op.invoke(cap.name, {"tenant": "t", "doc_id": "d"})
                assert out["status"] == "denied" and out["executed"] is False, (app, cap.name, out)
            checked[app] = len(op.manifest.capabilities)
        assert {"sky", "video-ad", "zoning"} <= set(checked) and all(v for v in checked.values()), checked

    def test_every_operator_constructor_in_the_repository_is_an_app_operator_or_an_exempt_kernel_operator(self):
        """Walk the whole repo: a new ``Operator(...)`` site must be consciously classified (an app operator, which the seam gates, or a documented exemption)."""
        known_dirs = {p.parent.name for p in APPS_DIR.glob("*/operator.py")}
        sites = []
        for base in (ROOT / "agentic_os", APPS_DIR, ROOT / "deploy", ROOT / "examples"):
            for f in sorted(base.rglob("*.py")):
                if "test" in f.name or "__pycache__" in f.parts:
                    continue
                try:
                    tree = ast.parse(f.read_text())
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("Operator", "GatedOperator") and n.args:
                        a0 = n.args[0]
                        name = a0.value if isinstance(a0, ast.Constant) else ast.unparse(a0)
                        sites.append((str(f.relative_to(ROOT)), name))
        for path, name in sites:
            rel = pathlib.Path(path)
            if rel.parts[0] == "apps" and rel.parts[2] == "operator.py":
                assert rel.parts[1] in known_dirs
                continue
            assert path in KERNEL_PLANE_SITES or path in DEMO_OPERATOR_SITES or "self.name" in name or name == "OPERATOR_NAME", (path, name)

    def test_the_demo_operator_sites_are_denied_under_permissions_because_they_have_no_gate(self, monkeypatch):
        monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
        op = Operator("agentic-billing", [capability("billing.refund", lambda i: {"ok": 1}, side_effecting=True)])
        assert op.invoke("billing.refund", {})["status"] == "denied"


# the three kernel plane modules that register their operator with app_gate.kernel_governed (pinned by apps/test_dr04_no_name_based_exemption.py)
KERNEL_PLANE_SITES = {"agentic_os/interaction/operator.py", "agentic_os/agent_gateway/social/operator.py", "agentic_os/agent_gateway/external/operator.py"}
# demos and pilots that build in-memory fakes under names that match no gate: denied under permissions (no gate registered), allowed otherwise
DEMO_OPERATOR_SITES = {"agentic_os/mission/pilots.py", "agentic_os/mission/unified_demo.py"}
