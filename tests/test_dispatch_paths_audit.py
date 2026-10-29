"""Dispatch-path audit: every way an app registers an agent tool is a KNOWN style with classified tools, and the Assistant consoles (books,
billing, compliance) consult the same authorizer seam as the harness registry when one is installed.

A new registration style, or a console that dispatches tool bodies without the seam, must fail this test so it is classified deliberately."""
from __future__ import annotations

import ast
import base64
import importlib.util
import pathlib
import sys

import pytest

from agentic_os.permissions import GrantStore, make_authorizer
from agentic_os.tool_actions import TOOL_ACTIONS, tool_actions

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = base64.b64encode(b"w" * 32).decode()
KNOWN_TOOL_CONSTRUCTORS = {"_crtool": "context_runtime AgentConsole (harness ToolRegistry seam)", "_Tool": "apps/*/assistant.py Assistant (authorizer= hook)"}
KNOWN_CONSOLES = {"_AgentConsole", "_Assistant"}


def _calls(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            yield (f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""), n


def test_every_tool_registration_style_in_the_apps_is_a_known_one():
    seen_tool, seen_console = {}, set()
    for f in sorted((ROOT / "apps").glob("*/*.py")):
        if "test" in f.name:
            continue
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for name, n in _calls(tree):
            if name in ("function_tool", "ToolPlugin", "ToolSpec", "FunctionTool", "Tool", "tool", "_tool", "register_tool"):
                seen_tool.setdefault(name, []).append(f"{f.parent.name}/{f.name}")
            if name.endswith("Console") or name.endswith("Assistant"):
                seen_console.add(name)
    assert seen_tool == {}, f"a tool registration style the classification does not know: {seen_tool}"
    assert seen_console <= KNOWN_CONSOLES, seen_console - KNOWN_CONSOLES


def test_tool_names_are_unique_across_apps_so_one_mapping_covers_them_all():
    names = [t for tools in TOOL_ACTIONS.values() for t in tools]
    assert len(names) == len(set(names))


def _load(app):
    spec = importlib.util.spec_from_file_location(f"assistant_{app}", ROOT / "apps" / app / "assistant.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m          # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(m)
    return m


class _P:
    def __init__(self, app="", user="u", roles=()):
        self.app, self.user, self.roles = app, user, roles


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    (tmp_path / "p").mkdir()
    return GrantStore(str(tmp_path / "p"))


@pytest.mark.parametrize("app", ["books", "billing", "compliance"])
class TestTheAssistantConsolesUseTheSameSeam:
    def _console(self, app, authorizer):
        m = _load(app)
        ran = []
        tool = m.Tool("zap_thing", "d", lambda a: (ran.append(1), {"text": "zapped"})[1], keywords=("zap",), side_effecting=True)
        return m.Assistant("t", "p", [tool], [], "s", (), authorizer=authorizer), ran

    def _gate(self, store):
        return make_authorizer(store, tool_resources={"zap_thing": ("table", "x")}, tool_actions={"zap_thing": "write"})

    def test_without_an_authorizer_behaviour_is_unchanged(self, app):
        c, ran = self._console(app, None)
        assert c.respond("zap the thing", principal=_P())["text"] == "zapped" and ran == [1]

    def test_a_read_only_principal_cannot_run_a_write_tool_and_the_body_never_runs(self, app, store):
        store.add("app", "u", "table", "x", actions=["read"])
        c, ran = self._console(app, self._gate(store))
        out = c.respond("zap the thing", principal=_P(app="u"))
        assert out.get("denied") is True and "[blocked] zap_thing" in out["text"] and ran == []

    def test_a_write_grant_lets_it_run(self, app, store):
        store.add("app", "u", "table", "x", actions=["read", "write"])
        c, ran = self._console(app, self._gate(store))
        assert c.respond("zap the thing", principal=_P(app="u"))["text"] == "zapped" and ran == [1]

    def test_no_principal_or_an_invalid_one_is_denied(self, app, store):
        store.add("app", "u", "table", "x", actions=["read", "write"])
        c, ran = self._console(app, self._gate(store))
        for p in (None, _P(app="u", user=None), _P(app="u", roles=None), _P(app=["u"])):
            assert c.respond("zap the thing", principal=p).get("denied") is True
        assert ran == []

    def test_an_authorizer_that_raises_is_a_denial(self, app):
        def boom(principal, spec, args):
            raise RuntimeError("x")
        c, ran = self._console(app, boom)
        assert c.respond("zap the thing", principal=_P()).get("denied") is True and ran == []

    def test_the_gate_is_not_consulted_for_a_message_that_picks_no_tool(self, app, store):
        calls = []
        c, _ = self._console(app, lambda p, s, a: calls.append(1))
        c.respond("hello there", principal=_P())
        assert calls == []


def test_the_real_tool_tables_of_the_three_consoles_work_with_the_gate(store):
    """Each console's real tool names are classified, so make_authorizer(tool_actions=tool_actions(app)) knows them."""
    for app in ("books", "billing", "compliance"):
        acts = tool_actions(app)
        assert acts and set(acts.values()) <= {"read", "write", "delete"}
        src = (ROOT / "apps" / app / "app.py").read_text()
        for tool in acts:
            if "." not in tool:                      # a console tool; a dotted name is an Operator SDK capability (checked in test_tool_actions_complete)
                assert f'_Tool("{tool}"' in src
    assert tool_actions("billing")["refund"] == "write" and tool_actions("billing")["chase_overdue"] == "write"
