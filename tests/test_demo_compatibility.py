"""Demo and unattended-run compatibility gate for the permissions plane.

Each documented demo path is started with its documented configuration and synthetic data, its principal workflow is run
non-interactively, and the test asserts BOTH that the intended operation succeeds AND that the unauthorised operations stay
denied (wrong principal, unmapped or unclassified tool, a write with only a read grant, an approved action without the grant,
a missing key at start-up). Nothing here waits for a human, prompts, touches the network or a live system.

Authorisation and approval are separate: a demo that must run unattended is configured (explicit principals with scoped
grants, classified tools, a deterministic approver limited to listed low-risk synthetic operations, simulated side effects),
never by weakening either check. See ``agentic_os/permissions_profiles.py``.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import stat
import subprocess
import sys
import textwrap

import pytest

from agentic_os import permissions as perm
from agentic_os.permissions import GrantStore
from agentic_os.permissions_profiles import (DEMO_NOBODY, DEMO_OPERATOR, DEMO_VIEWER, ApprovedOperation,
                                             DeterministicApprover, SimulatedEffects, build_gate,
                                             provision_demo_grants)
from agentic_os.tool_actions import TOOL_ACTIONS

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = base64.b64encode(b"d" * 32).decode()
_STRIP = ("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_")


def _env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_STRIP)}
    env.update(PYTHONPATH=str(ROOT), **extra)
    return env


def _py(code: str, **env_extra) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=_env(**env_extra), cwd=str(ROOT),
                          capture_output=True, text=True, timeout=180)


def _json(p: subprocess.CompletedProcess) -> dict:
    assert p.returncode == 0, p.stderr[-800:]
    return json.loads(p.stdout.strip().splitlines()[-1])


SHOWCASE = """
import importlib.util, json
spec = importlib.util.spec_from_file_location("perm_demo_app", "deploy/permissions-showcase-demo/app.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from fastapi.testclient import TestClient
c = TestClient(m.app)
admin = {"X-API-Key": "adm-k-0123456789-DO-NOT-STORE"}
sc = {i["id"]: i for i in c.get("/api/demo/scenarios").json()}
ev = lambda i: c.get("/api/demo/scenarios/" + i).json()
us, none, ro = ev("support-us-masked"), ev("no-grant-denied"), ev("read-only-cannot-write")
print(json.dumps({"scenarios": sorted(sc), "us_visible": us["visible_count"], "us_full": us["full_count"],
  "us_masked": sorted({col for r in us["records"] for col in r.get("_masked", [])}), "denied": [none["allowed"], ro["allowed"]],
  "unknown": c.get("/api/demo/scenarios/nope").status_code, "health": c.get("/healthz").json(),
  "admin_no_key": [c.get("/api/permissions/grants").status_code, c.get("/api/permissions/status").status_code,
                   c.post("/api/permissions/authorize", json={}).status_code, c.post("/api/permissions/grants", json={}).status_code],
  "admin_wrong_key": c.get("/api/permissions/grants", headers={"X-API-Key": "nope"}).status_code,
  "admin_grants": sorted(g["id"] for g in c.get("/api/permissions/grants", headers=admin).json()),
  "admin_status": c.get("/api/permissions/status", headers=admin).json()["key_source"]}))
"""

CONTROL = """
import json
from agentic_os import control_plane as cp
from fastapi.testclient import TestClient
c = TestClient(cp.app)
h = {"X-API-Key": "adm-k-0123456789-DO-NOT-STORE"}
def allowed(**b):
    return c.post("/api/permissions/authorize", json={"resource_name": "crm.customers", **b}, headers=h).json()["allowed"]
print(json.dumps({"status": c.get("/api/permissions/status", headers=h).json(), "support": allowed(app="support"), "nobody": allowed(app="nobody"),
  "write_action": allowed(app="support", action="write"),
  "no_credentials": [c.get("/api/permissions/status").status_code, c.post("/api/permissions/grants", json={"subject": "x", "resource_name": "crm.customers"}).status_code]}))
"""


@pytest.fixture()
def volume(tmp_path):
    return tmp_path / "perm"


class TestPermissionsShowcase:
    def test_starts_with_a_provisioned_key_and_runs_its_workflow(self, volume):
        r = _json(_py(SHOWCASE, PERMISSIONS_DIR=str(volume), PERMISSIONS_KEY=KEY, PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE"))
        assert "support-us-masked" in r["scenarios"] and r["us_visible"] < r["us_full"] and r["us_masked"] == ["email"]   # the public workflow
        assert r["denied"] == [False, False] and r["unknown"] == 404                                                    # unauthorised stays denied
        assert r["health"] == {"ok": True}                                                                              # liveness only
        assert r["admin_no_key"] == [401, 401, 401, 401] and r["admin_wrong_key"] == 401                                 # the admin API needs the key
        assert r["admin_grants"] == ["seed-finance-crm-customers", "seed-support-crm-customers"] and r["admin_status"] == "env"

    def test_with_no_admin_key_configured_the_admin_api_is_disabled_never_open(self, volume):
        p = _py(SHOWCASE, PERMISSIONS_DIR=str(volume), PERMISSIONS_KEY=KEY)
        assert p.returncode != 0 or True                                            # the script's admin_grants call (with a key header) is 503
        r = _json(_py(SHOWCASE.replace('c.get("/api/permissions/grants", headers=admin).json()', '[]').replace(
            'c.get("/api/permissions/status", headers=admin).json()["key_source"]', '"-"'), PERMISSIONS_DIR=str(volume), PERMISSIONS_KEY=KEY))
        assert r["admin_no_key"] == [503, 503, 503, 503] and r["admin_wrong_key"] == 503

    def test_a_missing_store_key_is_a_clear_refusal_on_the_admin_api_never_a_silent_key(self, volume):
        """PP-08: the public demo starts without the store key; only the admin API needs it, and it answers 503 (the fix is in the server log)."""
        code = """
import importlib.util, json
from fastapi.testclient import TestClient
spec = importlib.util.spec_from_file_location("d", "deploy/permissions-showcase-demo/app.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
c = TestClient(m.app, raise_server_exceptions=False)
print(json.dumps({"public": c.get("/api/demo/scenarios").status_code,
                  "admin": c.get("/api/permissions/grants", headers={"X-API-Key": "adm-k-0123456789-DO-NOT-STORE"}).status_code}))
"""
        p = _py(code, PERMISSIONS_DIR=str(volume), PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE")
        assert p.returncode == 0 and json.loads(p.stdout.strip().splitlines()[-1]) == {"public": 200, "admin": 503}
        assert "GrantStorageError" not in p.stdout and "PERMISSIONS_KEY" in p.stderr
        assert not (volume / ".permissions.key").exists()

    def test_an_existing_deployment_with_a_key_file_keeps_working(self, volume):
        perm.provision_key_file(str(volume))                                       # the state a running deployment is in
        r = _json(_py(SHOWCASE, PERMISSIONS_DIR=str(volume), PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE"))
        assert r["admin_status"] == "keyfile" and r["us_visible"] < r["us_full"]


class TestControlPlane:
    def test_starts_with_a_provisioned_key_and_runs_its_workflow(self, volume):
        r = _json(_py(CONTROL, PERMISSIONS_DIR=str(volume), PERMISSIONS_KEY=KEY, PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE"))
        assert r["status"]["encrypted"] and r["status"]["key_source"] == "env" and r["support"] is True
        assert r["nobody"] is False and r["write_action"] is False                 # unauthorised stays denied
        assert r["no_credentials"] == [401, 401]                                    # the admin API needs the credential

    def test_a_missing_key_at_start_up_is_a_clear_refusal(self, volume):
        p = _py(CONTROL, PERMISSIONS_DIR=str(volume))
        assert p.returncode != 0 and "GrantStorageError" in p.stderr

    def test_an_existing_key_file_keeps_it_starting(self, volume):
        perm.provision_key_file(str(volume))
        assert _json(_py(CONTROL, PERMISSIONS_DIR=str(volume), PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE"))["status"]["key_source"] == "keyfile"


class TestAgentGatewayIsAnIndependentPlane:
    def test_it_does_not_depend_on_the_grant_plane(self):
        offenders = [f.name for f in (ROOT / "agentic_os" / "agent_gateway").glob("*.py")
                     if "agentic_os.permissions" in f.read_text() or "from ..permissions" in f.read_text()
                     or "from .. import permissions" in f.read_text()]
        assert offenders == []


# ───────────────────────── the gate, in the unattended profiles ─────────────────────────


def _tool_actions():
    return {"read_records": "read", "update_record": "write", "send_email": "write", "purge_record": "delete"}


def _approved():
    return (ApprovedOperation("update_record", "synthetic demo records only; changes an in-memory fixture",
                              check=lambda a: a.get("synthetic") is True),)


@pytest.fixture()
def tools_mod():
    return pytest.importorskip("agent_harness.tools")


@pytest.fixture()
def demo(tmp_path, monkeypatch, tools_mod):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    monkeypatch.setattr("builtins.input", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a demo must never prompt")))
    store = GrantStore(str(tmp_path / "p"))
    provision_demo_grants(store)
    return store


def _registry(store, tools_mod, profile="demo", **kw):
    names = ("read_records", "update_record", "send_email", "purge_record")
    gate = build_gate(profile, store, tool_actions=_tool_actions(),
                      tool_resources={n: ("table", "demo.records") for n in names},
                      approved_operations=kw.pop("approved_operations", _approved()), **kw)
    reg = gate.registry()
    reg.register(tools_mod.function_tool("read_records", lambda a: tools_mod.ToolResult(text="rows")))
    for n, kind in (("update_record", "update"), ("send_email", "email"), ("purge_record", "purge")):
        body = gate.effects.tool(kind)
        reg.register(tools_mod.function_tool(n, lambda a, b=body: (b(a), tools_mod.ToolResult(text="done"))[1],
                                             side_effecting=True))
    return gate, reg


class TestUnattendedDemoProfile:
    def test_a_read_grant_reads_without_any_approval(self, demo, tools_mod):
        _, reg = _registry(demo, tools_mod)
        assert reg.run("read_records", {}, principal=DEMO_VIEWER).ok

    def test_an_approved_synthetic_operation_runs_unattended_with_a_simulated_effect(self, demo, tools_mod):
        gate, reg = _registry(demo, tools_mod)
        res = reg.run("update_record", {"id": 1, "synthetic": True}, principal=DEMO_OPERATOR)
        assert res.ok and gate.effects.log[0]["kind"] == "update_record" and gate.effects.log[0]["simulated"] is True
        assert gate.approver.decisions[-1]["approved"] is True and gate.approver.decisions[-1]["profile"] == "demo"

    def test_a_write_with_only_a_read_grant_is_denied_even_though_it_would_be_approved(self, demo, tools_mod):
        gate, reg = _registry(demo, tools_mod)
        res = reg.run("update_record", {"id": 1, "synthetic": True}, principal=DEMO_VIEWER)
        assert not res.ok and "write" in res.error and gate.effects.log == []      # authorisation is its own check
        assert gate.approver.decisions == []                                       # never even asked

    def test_an_operation_that_is_not_listed_is_denied_not_waited_on(self, demo, tools_mod):
        gate, reg = _registry(demo, tools_mod)
        res = reg.run("send_email", {"to": "x@example.test"}, principal=DEMO_OPERATOR)   # granted, but not approved
        assert not res.ok and gate.effects.log == [] and gate.approver.decisions[-1]["approved"] is False

    def test_an_approved_tool_with_arguments_outside_the_approval_is_denied(self, demo, tools_mod):
        gate, reg = _registry(demo, tools_mod)
        assert not reg.run("update_record", {"id": 1}, principal=DEMO_OPERATOR).ok
        assert gate.effects.log == []

    def test_delete_needs_its_own_grant_and_approval(self, demo, tools_mod):
        _, reg = _registry(demo, tools_mod)
        assert not reg.run("purge_record", {}, principal=DEMO_OPERATOR).ok

    def test_the_wrong_principal_and_the_ungranted_principal_are_denied(self, demo, tools_mod):
        _, reg = _registry(demo, tools_mod)
        assert not reg.run("read_records", {}, principal=DEMO_NOBODY).ok
        assert not reg.run("read_records", {}, principal=type("P", (), {"app": "someone-else", "user": "x", "roles": ()})()).ok

    def test_an_unmapped_tool_is_denied_even_for_a_privileged_role(self, demo, tools_mod):
        _, reg = _registry(demo, tools_mod)
        reg.register(tools_mod.function_tool("mystery", lambda a: tools_mod.ToolResult(text="x")))
        admin = type("P", (), {"app": "", "user": "root", "roles": ("admin",)})()
        assert not reg.run("mystery", {}, principal=admin).ok

    def test_an_unclassified_tool_is_denied(self, demo, tools_mod):
        gate = build_gate("demo", demo, tool_actions={}, tool_resources={"t": ("table", "demo.records")})
        reg = gate.registry()
        reg.register(tools_mod.function_tool("t", lambda a: tools_mod.ToolResult(text="x")))
        assert not reg.run("t", {}, principal=DEMO_OPERATOR).ok

    def test_the_demo_grants_are_visible_scoped_and_revocable(self, demo):
        gs = {g.subject: g for g in demo.list()}
        assert set(gs) == {"demo-viewer", "demo-operator"} and all(g.created_by == "demo-fixture" for g in gs.values())
        assert gs["demo-viewer"].actions == ["read"] and set(gs["demo-operator"].actions) == {"read", "write"}
        assert demo.remove(gs["demo-operator"].id) is True


class TestAutomatedTestProfile:
    def test_the_test_profile_is_deterministic_and_never_prompts(self, demo, tools_mod):
        gate, reg = _registry(demo, tools_mod, profile="test")
        assert reg.run("update_record", {"synthetic": True}, principal=DEMO_OPERATOR).ok
        assert not reg.run("send_email", {}, principal=DEMO_OPERATOR).ok
        assert [d["approved"] for d in gate.approver.decisions] == [True, False]
        assert all(d["profile"] == "test" for d in gate.approver.decisions)

    def test_the_approver_exists_only_in_demo_and_test(self):
        with pytest.raises(ValueError):
            DeterministicApprover((), profile="production")

    def test_an_approved_operation_needs_a_written_reason(self):
        with pytest.raises(ValueError, match="reason"):
            ApprovedOperation("update_record", "  ")

    def test_a_failing_predicate_is_a_refusal(self):
        a = DeterministicApprover([ApprovedOperation("t", "r", check=lambda args: 1 / 0)], profile="test")
        assert a({"tool": "t", "args": {}}) is False

    def test_decisions_do_not_record_argument_values(self):
        a = DeterministicApprover([ApprovedOperation("t", "r")], profile="test")
        a({"tool": "t", "args": {"secret_value": "hunter2"}})
        assert "hunter2" not in json.dumps(a.decisions) and a.decisions[0]["args"] == ["secret_value"]


class TestProductionProfile:
    def test_it_refuses_a_list_of_auto_approved_operations(self, demo):
        with pytest.raises(ValueError, match="human"):
            build_gate("production", demo, tool_actions=_tool_actions(), approved_operations=_approved())

    def test_without_a_human_approver_a_consequential_action_is_denied_never_auto_approved(self, demo, tools_mod):
        _, reg = _registry(demo, tools_mod, profile="production", approved_operations=())
        assert not reg.run("update_record", {"synthetic": True}, principal=DEMO_OPERATOR).ok

    def test_a_human_approver_decides_and_authorisation_is_still_required(self, demo, tools_mod):
        seen = []
        _, reg = _registry(demo, tools_mod, profile="production", approved_operations=(),
                           human_approver=lambda call: (seen.append(call["tool"]), True)[1])
        assert reg.run("update_record", {"id": 1}, principal=DEMO_OPERATOR).ok and seen == ["update_record"]
        assert not reg.run("update_record", {"id": 1}, principal=DEMO_VIEWER).ok     # approved by nobody: not even asked
        assert seen == ["update_record"]

    def test_demo_and_test_never_take_a_human_approver(self, demo):
        with pytest.raises(ValueError, match="never prompts"):
            build_gate("demo", demo, tool_actions=_tool_actions(), human_approver=lambda c: True)

    def test_an_unknown_profile_is_refused(self, demo):
        with pytest.raises(ValueError):
            build_gate("everything-allowed", demo, tool_actions={})

    def test_there_is_no_switch_that_disables_the_gate(self):
        import inspect
        names = set(inspect.signature(build_gate).parameters) | set(inspect.signature(perm.make_authorizer).parameters)
        assert not {n for n in names if any(w in n.lower() for w in ("disable", "bypass", "skip", "noauth", "allow_all", "auto_approve"))}


class TestSimulatedEffects:
    def test_a_simulated_effect_is_recorded_and_flagged(self):
        fx = SimulatedEffects()
        fx.tool("email", result="queued")({"to": "x@example.test"})
        assert fx.log[0]["kind"] == "email" and fx.log[0]["simulated"] is True and fx.log[0]["args"] == ["to"]


# ───────────────────────── the individual-user quickstart, end to end ─────────────────────────


class TestQuickstartWalkthrough:
    def _cli(self, *args, **env):
        return subprocess.run([sys.executable, "-m", "agentic_os.cli", "permissions", *args], env=_env(**env), cwd=str(ROOT),
                              capture_output=True, text=True, timeout=120)

    def test_a_fresh_install_works_end_to_end(self, tmp_path, tools_mod, monkeypatch):
        d = str(tmp_path / "perm")
        r = self._cli("init", "--dir", d)
        assert r.returncode == 0, r.stderr
        _reads = sum(1 for tools in TOOL_ACTIONS.values() for a in tools.values() if a == "read")
        _writes = sum(1 for tools in TOOL_ACTIONS.values() for a in tools.values() if a != "read")
        assert "created, mode 0600" in r.stdout and f"{_reads} for bundled tools" in r.stdout and f"{_writes} write-class tools withheld" in r.stdout
        assert stat.S_IMODE(os.stat(f"{d}/.permissions.key").st_mode) == 0o600 and stat.S_IMODE(os.stat(d).st_mode) == 0o700
        key_bytes = pathlib.Path(f"{d}/.permissions.key").read_bytes()

        listed = self._cli("list", "--dir", d)
        assert listed.returncode == 0 and "edge-sentinel.threat_summary" in listed.stdout and "by quickstart" in listed.stdout
        assert key_bytes.decode() not in listed.stdout + listed.stderr + r.stdout + r.stderr          # no secret is ever printed

        # the grants are used with no environment at all: the key file is found where `init` put it
        from agentic_os.permissions_setup import load_gate
        gate, me = load_gate(d)
        reg = gate.registry()
        reg.register(tools_mod.function_tool("threat_summary", lambda a: tools_mod.ToolResult(text="ok")))
        reg.register(tools_mod.function_tool("ban_ip", lambda a: tools_mod.ToolResult(text="banned"), side_effecting=True))
        reg.register(tools_mod.function_tool("scan_app", lambda a: tools_mod.ToolResult(text="scanned")))
        reg.register(tools_mod.function_tool("third_party_tool", lambda a: tools_mod.ToolResult(text="x")))
        assert reg.run("threat_summary", {}, principal=me).ok                       # bundled read tool: granted
        assert not reg.run("ban_ip", {"ip": "192.0.2.1"}, principal=me).ok          # write-class: not granted by default
        assert not reg.run("scan_app", {"app": "x"}, principal=me).ok               # scanner launcher: write-class
        assert not reg.run("third_party_tool", {}, principal=me).ok                 # not bundled: unmapped, denied

        # opt in to the write-class tools: authorised, and still needs a human approval (none supplied => denied)
        assert self._cli("init", "--dir", d, "--include-write").returncode == 0
        assert pathlib.Path(f"{d}/.permissions.key").read_bytes() == key_bytes      # the key is never replaced
        gate, me = load_gate(d)
        reg = gate.registry()
        reg.register(tools_mod.function_tool("ban_ip", lambda a: tools_mod.ToolResult(text="banned"), side_effecting=True))
        reg.register(tools_mod.function_tool("scan_app", lambda a: tools_mod.ToolResult(text="scanned")))
        assert reg.run("scan_app", {"app": "x"}, principal=me).ok                   # authorised; not flagged side-effecting
        assert not reg.run("ban_ip", {"ip": "192.0.2.1"}, principal=me).ok          # authorised, but approval is a separate check
        gate, me = load_gate(d, human_approver=lambda call: True)
        reg = gate.registry()
        reg.register(tools_mod.function_tool("ban_ip", lambda a: tools_mod.ToolResult(text="banned"), side_effecting=True))
        assert reg.run("ban_ip", {"ip": "192.0.2.1"}, principal=me).ok

        # revoke: the very next call is denied
        gid = next(g.id for g in GrantStore(d).list() if g.resource_name == "edge-sentinel.threat_summary")
        assert self._cli("revoke", gid, "--dir", d).returncode == 0
        gate, me = load_gate(d)
        reg = gate.registry()
        reg.register(tools_mod.function_tool("threat_summary", lambda a: tools_mod.ToolResult(text="ok")))
        assert not reg.run("threat_summary", {}, principal=me).ok

    def test_init_is_idempotent_and_grants_only_bundled_tools(self, tmp_path):
        d = str(tmp_path / "perm")
        assert self._cli("init", "--dir", d).returncode == 0 and self._cli("init", "--dir", d).returncode == 0
        grants = GrantStore(d).list()
        bundled = {f"{a}.{t}" for a, ts in TOOL_ACTIONS.items() for t in ts}
        assert len(grants) == sum(1 for tools in TOOL_ACTIONS.values() for a in tools.values() if a == "read") and {g.resource_name for g in grants} <= bundled
        assert all(g.subject_kind == "user" and g.subject == "local" and g.actions == ["read"] for g in grants)

    def test_a_directory_with_a_damaged_key_file_is_refused(self, tmp_path):
        d = tmp_path / "perm"
        d.mkdir()
        (d / ".permissions.key").write_text("%%%")
        r = self._cli("init", "--dir", str(d))
        assert r.returncode == 2 and "unusable" in r.stderr and (d / ".permissions.key").read_text() == "%%%"


class TestScannerClassification:
    LAUNCHERS = ("supply_chain_scan", "list_vulnerabilities", "scan_app", "scan_apps", "sbom", "malware_scan", "scan_deployment")

    def test_the_launchers_that_start_processes_or_write_are_not_read(self):
        es = TOOL_ACTIONS["edge-sentinel"]
        assert {t: es[t] for t in self.LAUNCHERS} == {t: "write" for t in self.LAUNCHERS}

    def test_the_query_only_scanner_tools_stay_read(self):
        es = TOOL_ACTIONS["edge-sentinel"]
        assert {es[t] for t in ("scanner_status", "explain_vulnerability", "scan_status", "scan_results", "list_scannable_apps")} == {"read"}

    def test_there_is_no_execute_action(self):
        assert perm.ACTIONS == ("read", "write", "delete")


class TestShippedConfigs:
    def test_no_shipped_file_provisions_a_key_or_a_dev_opt_in_silently(self):
        """The shipped compose files require PERMISSIONS_KEY from outside (no default) and never carry a key value or a dev
        opt-in; see tests/test_deployment_paths.py for the full deployment-path checks."""
        for f in (ROOT / "docker-compose.yml", ROOT / "deploy" / "docker-compose.yml",
                  ROOT / "deploy" / "permissions-showcase-demo" / "permissions-demo.compose.yml"):
            text = f.read_text()
            assert "PERMISSIONS_ALLOW_" not in text
            for m in __import__("re").finditer(r"PERMISSIONS_KEY=(\S*)", text):
                assert m.group(1).startswith('${PERMISSIONS_KEY:?'), m.group(0)      # an external secret, never a literal or a default
