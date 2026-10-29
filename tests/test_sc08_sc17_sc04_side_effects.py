"""SC-08, SC-17, SC-04 from the 2026-10 AI-assisted permissions security review.

SC-08: the "registered read but declares side effects" rule also applies to PUBLIC tools (a public tool whose spec declares
side_effecting is ambiguous and denied).
SC-17: the six scanner launchers are classified write AND registered side_effecting=True in the trusted registration, so the approval
policy applies IN ADDITION to the write grant (authorisation and approval stay separate checks).
SC-04: the profiles do not make side effects "simulated" by themselves; tests opt into the `simulated_effects` helper, which replaces a
registered tool body with a recorded stub that can never run the real one.
"""
from __future__ import annotations

import ast
import base64
import pathlib
import re

import pytest

from agentic_os.permissions import GrantStore, make_authorizer
from agentic_os.permissions_profiles import (ApprovedOperation, DemoPrincipal, SimulatedEffects, build_gate, simulated_effects)
from agentic_os.tool_actions import TOOL_ACTIONS, tool_actions

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = base64.b64encode(b"x" * 32).decode()
LAUNCHERS = ("supply_chain_scan", "list_vulnerabilities", "scan_app", "scan_apps", "sbom", "malware_scan")
tools_mod = pytest.importorskip("agent_harness.tools")


class _Spec:
    def __init__(self, name, **kw):
        self.name = name
        self.__dict__.update(kw)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    (tmp_path / "p").mkdir()
    return GrantStore(str(tmp_path / "p"))


class TestSC08PublicToolsAreCheckedForAmbiguity:
    def test_a_public_tool_that_declares_side_effects_is_denied_even_for_no_principal(self, store):
        a = make_authorizer(store, public_tools=["p"], tool_actions={"p": "read"})
        reason = a(None, _Spec("p", side_effecting=True), {})
        assert reason is not None and "ambiguous" in reason

    def test_a_public_tool_that_declares_nothing_is_still_allowed(self, store):
        a = make_authorizer(store, public_tools=["p"], tool_actions={"p": "read"})
        assert a(None, _Spec("p"), {}) is None and a(None, _Spec("p", side_effecting=False), {}) is None

    def test_a_dict_spec_is_checked_the_same_way(self, store):
        a = make_authorizer(store, public_tools=["p"], tool_actions={"p": "read"})
        assert a(None, {"name": "p", "side_effecting": True}, {}) is not None

    def test_the_mapped_read_tool_rule_is_unchanged(self, store):
        a = make_authorizer(store, tool_resources={"t": ("table", "x")}, tool_actions={"t": "read"})
        assert a(None, _Spec("t", side_effecting=True), {}) is not None


def _registered_flags():
    """{tool: side_effecting} for every _crtool registration under apps/ (AST)."""
    out = {}
    for f in sorted((ROOT / "apps").glob("*/**/*.py")):
        if "test" in f.name:
            continue
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("_crtool", "_Tool") and n.args \
                    and isinstance(n.args[0], ast.Constant):
                out[n.args[0].value] = any(k.arg == "side_effecting" and isinstance(k.value, ast.Constant) and k.value.value is True
                                           for k in n.keywords)
    return out


class TestSC17ScannersAreApprovalGatedToo:
    def test_the_six_launchers_are_registered_side_effecting(self):
        flags = _registered_flags()
        assert {t: flags[t] for t in LAUNCHERS} == {t: True for t in LAUNCHERS}

    def test_every_tool_classified_write_is_registered_side_effecting(self):
        flags = _registered_flags()
        wrong = [(app, t) for app, tools in TOOL_ACTIONS.items() for t, a in tools.items() if a == "write" and t in flags and not flags[t]]
        assert wrong == []

    def test_no_read_tool_is_registered_side_effecting(self):
        flags = _registered_flags()
        assert [t for app, tools in TOOL_ACTIONS.items() for t, a in tools.items() if a == "read" and flags.get(t)] == []

    def _gate(self, store, profile, **kw):
        ta = tool_actions("edge-sentinel")
        names = ("supply_chain_scan", "list_scannable_apps")
        gate = build_gate(profile, store, tool_actions={n: ta[n] for n in names},
                          tool_resources={n: ("table", "edge") for n in names}, **kw)
        ran = []
        reg = gate.registry()
        reg.register(tools_mod.function_tool("supply_chain_scan", lambda a: (ran.append("scan"), tools_mod.ToolResult(text="ok"))[1],
                                             side_effecting=True))
        reg.register(tools_mod.function_tool("list_scannable_apps", lambda a: tools_mod.ToolResult(text="apps")))
        return gate, reg, ran

    ME = DemoPrincipal(app="edge-user", user="u", roles=())

    def test_production_without_a_human_approver_denies_a_scan_even_with_the_write_grant(self, store):
        store.add("app", "edge-user", "table", "edge", actions=["read", "write"])
        _, reg, ran = self._gate(store, "production")
        res = reg.run("supply_chain_scan", {}, principal=self.ME)
        assert not res.ok and ran == [] and "approval" in res.error
        assert reg.run("list_scannable_apps", {}, principal=self.ME).ok                  # a read tool needs no approval

    def test_production_with_a_human_approver_runs_it(self, store):
        store.add("app", "edge-user", "table", "edge", actions=["read", "write"])
        _, reg, ran = self._gate(store, "production", human_approver=lambda call: call["tool"] == "supply_chain_scan")
        assert reg.run("supply_chain_scan", {}, principal=self.ME).ok and ran == ["scan"]

    @pytest.mark.parametrize("profile", ["demo", "test"])
    def test_the_unattended_profiles_do_not_auto_approve_a_scan_unless_it_is_explicitly_listed(self, store, profile):
        store.add("app", "edge-user", "table", "edge", actions=["read", "write"])
        gate, reg, ran = self._gate(store, profile)
        assert not reg.run("supply_chain_scan", {}, principal=self.ME).ok and ran == []
        assert gate.approver.decisions[-1]["approved"] is False
        gate2, reg2, ran2 = self._gate(store, profile,
                                       approved_operations=[ApprovedOperation("supply_chain_scan", "a canned scan of fixtures")])
        res2 = reg2.run("supply_chain_scan", {}, principal=self.ME)
        assert res2.ok and ran2 == [] and res2.text == "[simulated] supply_chain_scan"       # approved: a recorded stub ran, not trivy

    def test_an_approval_never_substitutes_for_the_write_grant(self, store):
        store.add("app", "edge-user", "table", "edge", actions=["read"])                  # read only
        _, reg, ran = self._gate(store, "production", human_approver=lambda call: True)
        res = reg.run("supply_chain_scan", {}, principal=self.ME)
        assert not res.ok and ran == [] and "write" in res.error


class TestSC04TheProfileSubstitutesTheRealBody:
    """Release blocker: a deterministic approval decision is not a simulation. Under demo/test the registry the PROFILE builds runs a
    recorded stub for every side-effecting tool; the real body never runs, even with an ApprovedOperation plus a write grant."""

    @pytest.mark.parametrize("profile", ["demo", "test"])
    def test_the_real_body_never_runs_even_with_an_approved_operation_and_a_write_grant(self, store, profile):
        store.add("app", "u", "table", "x", actions=["read", "write"])
        gate = build_gate(profile, store, tool_actions={"ban": "write"}, tool_resources={"ban": ("table", "x")},
                          approved_operations=[ApprovedOperation("ban", "synthetic")])
        reg, sentinel = gate.registry(), []
        reg.register(tools_mod.function_tool("ban", lambda a: (sentinel.append("REAL BODY RAN"), tools_mod.ToolResult(text="REAL"))[1],
                                             side_effecting=True))
        res = reg.run("ban", {"ip": "192.0.2.1"}, principal=DemoPrincipal(app="u", user="u"))
        assert res.ok and sentinel == [] and res.text == "[simulated] ban"
        assert gate.effects.log == [{"kind": "ban", "simulated": True, "args": ["ip"]}]

    def test_a_write_classified_tool_is_stubbed_even_if_its_spec_forgot_the_flag(self, store):
        store.add("app", "u", "table", "x", actions=["read", "write"])
        gate = build_gate("demo", store, tool_actions={"purge": "write"}, tool_resources={"purge": ("table", "x")},
                          approved_operations=[ApprovedOperation("purge", "synthetic")])
        reg, sentinel = gate.registry(), []
        reg.register(tools_mod.function_tool("purge", lambda a: (sentinel.append(1), tools_mod.ToolResult(text="REAL"))[1]))
        assert reg.run("purge", {}, principal=DemoPrincipal(app="u", user="u")).ok and sentinel == []

    def test_a_read_tool_runs_its_real_body(self, store):
        store.add("app", "u", "table", "x", actions=["read"])
        gate = build_gate("demo", store, tool_actions={"look": "read"}, tool_resources={"look": ("table", "x")})
        reg = gate.registry()
        reg.register(tools_mod.function_tool("look", lambda a: tools_mod.ToolResult(text="REAL READ")))
        assert reg.run("look", {}, principal=DemoPrincipal(app="u", user="u")).text == "REAL READ" and gate.effects.log == []

    def test_the_stub_keeps_the_spec_so_approval_and_authorisation_still_apply(self, store):
        store.add("app", "u", "table", "x", actions=["read", "write"])
        gate = build_gate("test", store, tool_actions={"ban": "write"}, tool_resources={"ban": ("table", "x")})     # nothing approved
        reg = gate.registry()
        reg.register(tools_mod.function_tool("ban", lambda a: tools_mod.ToolResult(text="REAL"), side_effecting=True, description="d",
                                             parameters={"type": "object", "properties": {"ip": {"type": "string"}}}))
        spec = reg.get("ban").spec()
        assert spec.side_effecting is True and spec.description == "d" and "ip" in spec.parameters["properties"]
        assert not reg.run("ban", {}, principal=DemoPrincipal(app="u", user="u")).ok            # not approved: denied
        assert not reg.run("ban", {}, principal=DemoPrincipal(app="other", user="u")).ok        # not granted: denied
        assert gate.effects.log == []

    def test_safe_synthetic_is_the_only_exception_and_is_trusted_metadata_supplied_to_the_gate(self, store):
        store.add("app", "u", "table", "x", actions=["read", "write"])
        gate = build_gate("demo", store, tool_actions={"fixture_write": "write"}, tool_resources={"fixture_write": ("table", "x")},
                          approved_operations=[ApprovedOperation("fixture_write", "synthetic")], safe_synthetic=["fixture_write"])
        reg, ran = gate.registry(), []
        reg.register(tools_mod.function_tool("fixture_write", lambda a: (ran.append(1), tools_mod.ToolResult(text="FIXTURE"))[1],
                                             side_effecting=True))
        assert reg.run("fixture_write", {}, principal=DemoPrincipal(app="u", user="u")).text == "FIXTURE" and ran == [1]

    def test_a_tool_cannot_mark_itself_safe_through_its_own_spec(self, store):
        store.add("app", "u", "table", "x", actions=["read", "write"])
        gate = build_gate("demo", store, tool_actions={"t": "write"}, tool_resources={"t": ("table", "x")},
                          approved_operations=[ApprovedOperation("t", "synthetic")])
        reg, ran = gate.registry(), []

        class Sneaky:
            def spec(self):
                s = tools_mod.ToolSpec(name="t", side_effecting=True)
                object.__setattr__(s, "safe_synthetic", True)            # a self-description: ignored
                return s

            def run(self, args):
                ran.append(1)
                return tools_mod.ToolResult(text="REAL")
        reg.register(Sneaky())
        assert reg.run("t", {}, principal=DemoPrincipal(app="u", user="u")).text == "[simulated] t" and ran == []

    def test_production_runs_real_bodies_behind_a_human_approval_and_refuses_safe_synthetic(self, store):
        store.add("app", "u", "table", "x", actions=["read", "write"])
        with pytest.raises(ValueError, match="safe_synthetic"):
            build_gate("production", store, tool_actions={}, safe_synthetic=["t"])
        gate = build_gate("production", store, tool_actions={"ban": "write"}, tool_resources={"ban": ("table", "x")},
                          human_approver=lambda call: True)
        reg, ran = gate.registry(), []
        reg.register(tools_mod.function_tool("ban", lambda a: (ran.append(1), tools_mod.ToolResult(text="REAL"))[1], side_effecting=True))
        assert reg.run("ban", {}, principal=DemoPrincipal(app="u", user="u")).text == "REAL" and ran == [1]

    def test_the_explicit_helper_still_works_for_a_registry_built_elsewhere(self, store):
        reg = tools_mod.ToolRegistry()
        ran = []
        reg.register(tools_mod.function_tool("t", lambda a: (ran.append(1), tools_mod.ToolResult(text="REAL"))[1], side_effecting=True))
        fx = simulated_effects(reg, ["t"])
        reg.policy = tools_mod.ApprovalPolicy("bypass")
        assert reg.run("t", {"a": 1}).text == "[simulated] t" and ran == [] and fx.log[0]["kind"] == "t"
        with pytest.raises(tools_mod.ToolError):
            simulated_effects(reg, ["nope"])

    def test_the_profile_docs_state_the_substitution(self):
        text = (ROOT / "docs" / "PERMISSIONS_MIGRATION.md").read_text()
        section = text[text.index("## 10. Execution profiles"):text.index("## 11.")]
        assert "never the real" in section and "safe_synthetic" in section and "not a simulation" in section
