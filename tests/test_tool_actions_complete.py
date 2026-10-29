"""Every tool this repository registers has an explicit, reviewed action classification.

``agentic_os.tool_actions.TOOL_ACTIONS`` is the trusted registration metadata ``make_authorizer`` reads. This guard
fails when an app registers a tool that is missing from it (it would be denied at the gate), when an entry no longer
matches a registered tool, or when a tool registered ``side_effecting=True`` is classified ``read``.
"""
from __future__ import annotations

import ast
import pathlib

from agentic_os.permissions import ACTIONS
from agentic_os.tool_actions import TOOL_ACTIONS, tool_actions

APPS = pathlib.Path(__file__).resolve().parents[1] / "apps"


def _registered() -> dict[str, dict[str, bool]]:
    """{app: {tool: side_effecting}} for every ``_crtool("name", ...)`` / ``_Tool("name", ...)`` registration under apps/*/."""
    out: dict[str, dict[str, bool]] = {}
    for f in sorted(APPS.glob("*/**/*.py")):
        if "test" in f.name:
            continue
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("_crtool", "_Tool")
                    and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
                side = any(k.arg == "side_effecting" and isinstance(k.value, ast.Constant) and k.value.value is True
                           for k in n.keywords)
                out.setdefault(f.relative_to(APPS).parts[0], {})[n.args[0].value] = side
    return out


def test_the_scan_finds_the_registered_tools():
    assert sum(len(v) for v in _registered().values()) >= 40


def test_every_registered_tool_is_classified():
    missing = {app: sorted(set(tools) - set(TOOL_ACTIONS.get(app, {}))) for app, tools in _registered().items()}
    assert {a: m for a, m in missing.items() if m} == {}


def _operator_capabilities() -> dict[str, set]:
    """{app: {capability}} from every ``capability("name", ...)`` in apps/<app>/operator.py (the Operator SDK execution seam)."""
    out: dict[str, set] = {}
    for f in sorted(APPS.glob("*/operator.py")):
        for n in ast.walk(ast.parse(f.read_text())):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "capability" and n.args
                    and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
                out.setdefault(f.parent.name, set()).add(n.args[0].value)
    return out


def _quoted_in_app(app: str, tool: str) -> bool:
    """The tool name appears as a string literal in the app's non-test code: a console tool, a capability, or the tool a route asks the gate for."""
    needle = (f'"{tool}"', f"'{tool}'")
    return any(any(n in f.read_text() for n in needle) for f in (APPS / app).glob("*.py") if "test" not in f.name)


def test_no_classification_outlives_its_tool():
    reg, caps = _registered(), _operator_capabilities()
    stale = {app: sorted(t for t in tools if t not in reg.get(app, {}) and t not in caps.get(app, set()) and not _quoted_in_app(app, t))
             for app, tools in TOOL_ACTIONS.items()}
    assert {a: m for a, m in stale.items() if m} == {}


def test_every_operator_capability_is_classified():
    """The Operator SDK is a dispatch path too: a capability missing from the table would be denied under basic permissions."""
    gated = {p.parent.name for p in APPS.glob("*/app.py") if "install_gate" in p.read_text()}
    missing = {app: sorted(c - set(TOOL_ACTIONS.get(app, {}))) for app, c in _operator_capabilities().items() if app in gated}
    assert {a: m for a, m in missing.items() if m} == {}


def test_the_operators_outside_the_gate_are_exactly_the_ones_without_an_app_module():
    """sky, video-ad and zoning ship an operator but no ``app.py`` (they are mounted elsewhere), so nothing installs a gate for them: basic permissions
    do not reach them. Listed so a new operator is noticed (it must call install_gate, or be added here with the reason)."""
    gated = {p.parent.name for p in APPS.glob("*/app.py") if "install_gate" in p.read_text()}
    assert sorted(set(_operator_capabilities()) - gated) == ["sky", "video-ad", "zoning"]


def test_every_action_is_a_known_grant_action():
    assert {a for tools in TOOL_ACTIONS.values() for a in tools.values()} <= set(ACTIONS)


def test_a_tool_registered_side_effecting_is_never_classified_read():
    wrong = [(app, t) for app, tools in _registered().items() for t, side in tools.items()
             if side and TOOL_ACTIONS[app][t] == "read"]
    assert wrong == []


def test_tool_actions_returns_a_copy():
    m = tool_actions("edge-sentinel")
    m["ban_ip"] = "read"
    assert TOOL_ACTIONS["edge-sentinel"]["ban_ip"] == "write" and tool_actions("no-such-app") == {}


def test_every_public_candidate_is_a_registered_tool_listed_as_read():
    """A public tool is not exempt from classification: it is listed in TOOL_ACTIONS too, as read."""
    from agentic_os.tool_actions import PUBLIC_CANDIDATES
    reg = _registered()
    for app, tool in PUBLIC_CANDIDATES:
        assert tool in reg.get(app, {}) and TOOL_ACTIONS[app][tool] == "read"


def test_the_registrations_are_all_covered_so_the_gate_denies_none_of_them_for_want_of_a_listing():
    """Every one of the registered tools is in TOOL_ACTIONS (so `make_authorizer(tool_actions=tool_actions(app))` knows it)."""
    reg = _registered()
    assert sum(len(v) for v in reg.values()) == 46
    assert all(set(tools) <= set(TOOL_ACTIONS[app]) for app, tools in reg.items())
