"""A grant on ``infra`` covers ``infra`` and its real children (``infra.state``, ``infra.state.x``), and nothing else.

Coverage used to be ``startswith(grant + '.')``, so a grant on ``infra`` also covered ``infra.``, ``infra..x``, ``infra.x.``, ``infra...`` and ``infra.`` + NUL / tab / newline / zero-width: a path with an EMPTY (or
blank / control) segment is not a child. The enterprise plane (Python and Go) had the same defect and was fixed; this is the kernel plane, for every resource kind, the tool seam of the fleet apps included.
"""
from __future__ import annotations

import pytest

from agentic_os import app_gate
from agentic_os.permissions import Grant, GrantStore, Identity, PermissionsPlane, make_authorizer

KINDS = ["database", "table", "corpus", "tool"]
CHILDREN = ["infra", "infra.state", "infra.state.x", "infra.a.b.c", "infra.state-2", "infra.Zustand"]
NOT_CHILDREN = ["infra.", "infra..x", "infra.x.", "infra...", "infra..", "infra.x..y", "infra.\x00", "infra.\t", "infra.\n", "infra.\r", "infra.​", "infra.x​",
                "infra.x\x00y", "infra. ", "infra. x", "infra.x ", "infra. ", "infra.﻿", ".infra", "infra2", "infra x", "INFRA.x", "infra/state", "infra\x00.x", "xinfra.x", ""]


def grant(name="infra", kind="tool"):
    return Grant(id="g-1", subject_kind="role", subject="r", resource_kind=kind, resource_name=name, actions=["read", "write"])


@pytest.mark.parametrize("name", CHILDREN)
def test_the_resource_and_its_real_children_are_covered(name):
    assert grant().covers(name) is True


@pytest.mark.parametrize("name", NOT_CHILDREN)
def test_a_path_with_an_empty_or_unprintable_segment_is_not_a_child(name):
    assert grant().covers(name) is False, repr(name)


def test_exact_equality_is_unchanged_even_for_an_odd_grant_name():
    assert grant("a.b").covers("a.b") is True and grant("a.b").covers("a.b.c") is True and grant("a.b").covers("a") is False and grant("a.b").covers("a.bc") is False


@pytest.mark.parametrize("kind", KINDS)
def test_the_plane_decides_the_same_for_every_resource_kind(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("PERMISSIONS_KEY", "k" * 32)
    store = GrantStore(str(tmp_path / "p"))
    store.add("role", "r", kind, "infra", actions=["read", "write"])
    plane = PermissionsPlane(store, privileged_roles=())
    who = Identity(app="", user="u", roles=["r"])
    for name in CHILDREN:
        assert plane.authorize(who, kind, name, "read").allowed, name
    for name in NOT_CHILDREN:
        assert not plane.authorize(who, kind, name, "read").allowed, repr(name)
    other = "table" if kind != "table" else "database"
    assert not plane.authorize(who, other, "infra.state", "read").allowed                      # the kind still has to match


def test_a_grant_on_the_empty_child_itself_does_not_widen_to_the_parent_or_siblings(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", "k" * 32)
    store = GrantStore(str(tmp_path / "p"))
    store.add("role", "r", "tool", "infra.", actions=["read"])
    plane = PermissionsPlane(store, privileged_roles=())
    who = Identity(app="", user="u", roles=["r"])
    assert not plane.authorize(who, "tool", "infra", "read").allowed and not plane.authorize(who, "tool", "infra.state", "read").allowed


def test_the_tool_seam_of_a_fleet_app_with_an_app_wide_grant(tmp_path, monkeypatch):
    """An app-wide grant on ``infra`` authorises the app's real tools through the kernel authorizer and no name built from an empty segment."""
    monkeypatch.setenv("PERMISSIONS_KEY", "k" * 32)
    store = GrantStore(str(tmp_path / "p"))
    store.add("user", "u", "tool", "infra", actions=["read"])
    table = {"infra.verify": "read", "infra.provision": "write"}
    auth = make_authorizer(store, tool_resources={t: ("tool", t) for t in table}, tool_actions=table, privileged_roles=())
    ident = Identity(app="", user="u", roles=[])

    def spec(name):
        return type("S", (), {"name": name, "side_effecting": table.get(name) == "write", "approval_required": False})()
    assert auth(ident, spec("infra.verify"), {}) is None                                      # a real child, read granted
    assert auth(ident, spec("infra.provision"), {}) is not None                               # write was not granted
    for bad in ("infra.", "infra..verify", "infra.verify.", "infra...", "infra.\x00", "infra.\t", "infra.​", ".infra", "infra.verify\n"):
        assert auth(ident, spec(bad), {}) is not None, repr(bad)


@pytest.mark.parametrize("app", ["infra", "document-editor", "books", "guide"])
def test_the_basic_gate_denies_names_with_an_empty_segment_even_with_every_grant(tmp_path, monkeypatch, app):
    from agentic_os.permissions_setup import init_local_profile
    for k in ("CR_PERMISSIONS", "AGENTIC_OS_PERMISSIONS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    init_local_profile(str(tmp_path / "perm"), include_write=True)
    app_gate.reset_state()
    app_gate.install_gate(app)
    from agentic_os.tool_actions import TOOL_ACTIONS
    for tool in TOOL_ACTIONS[app]:
        assert app_gate.tool_denial(app, tool) is None, tool
        for bad in (tool + ".", tool + "..x", "." + tool, tool + ".\x00", tool + "\t", tool.replace(".", ".."), tool.replace(".", ".​")):
            if bad == tool:
                continue                                                                      # (a tool name with no dot: the replacements change nothing)
            assert app_gate.tool_denial(app, bad) is not None, repr(bad)
    app_gate.reset_state()
