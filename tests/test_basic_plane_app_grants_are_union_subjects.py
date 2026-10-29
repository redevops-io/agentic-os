"""AGPL basic plane: ``app`` grants are union subjects; the principal is the local profile; the fleet app/principal two-gate model is Enterprise.

In the kernel plane a grant's subject (``app``, ``role`` or ``user``) is one more way to be matched: the grants that match the identity are UNIONed, so a grant whose subject is an app allows every identity
that carries that app. The enterprise plane additionally requires the principal's own grant for the app's tools (the fleet template). Pinned here, for the apps that run under BASIC permissions:

* the basic gate's principal is the local profile user with an EMPTY ``app`` and no roles, taken from nothing a request carries, so an ``app``-subject grant (which needs ``identity.app`` to equal its subject)
  never matches it, whatever the caller sends;
* ``agentic-os permissions init`` creates only ``user``-subject, per-tool grants;
* a caller that builds an ``Identity(app=...)`` itself and asks the PLANE directly (an integrator, not an app) IS allowed by an app grant alone: documented, not reachable through the apps.
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from agentic_os import app_gate
from agentic_os.permissions import GrantStore, Identity, PermissionsPlane
from agentic_os.permissions_setup import init_local_profile
from agentic_os.tool_actions import TOOL_ACTIONS


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "AGENTIC_OS_PERMISSIONS", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    app_gate.reset_state()
    yield
    app_gate.reset_state()


def test_init_creates_only_user_subject_per_tool_grants(tmp_path):
    init_local_profile(str(tmp_path / "perm"), include_write=True)
    grants = GrantStore(str(tmp_path / "perm")).list()
    assert grants and {g.subject_kind for g in grants} == {"user"}
    assert all(g.resource_kind == "tool" and g.resource_name.count(".") >= 1 and len(g.actions) == 1 for g in grants)       # one tool, one action: nothing app-wide


def test_an_app_subject_grant_alone_opens_nothing_to_the_local_profile_or_to_any_caller(tmp_path, monkeypatch):
    """A grant provisioned (through the API or by hand) for subject_kind=app, covering the whole app with every action, does not widen what the local profile may do."""
    init_local_profile(str(tmp_path / "perm"), user="alice")                              # the profile: alice, read grants only
    store = GrantStore(str(tmp_path / "perm"))
    store.add("app", "books", "tool", "books", actions=["read", "write", "delete"])       # an app-wide grant on every books tool
    app_gate.install_gate("books")
    gate = app_gate.gate_for("books")
    assert gate.principal.app == "" and gate.principal.user == "alice" and list(gate.principal.roles) == []
    writes = [t for t, a in TOOL_ACTIONS["books"].items() if a != "read"]
    assert writes
    for tool in writes:
        assert app_gate.tool_denial("books", tool) is not None, tool                       # still denied: only alice's own (read) grants count
    for tool in (t for t, a in TOOL_ACTIONS["books"].items() if a == "read"):
        assert app_gate.tool_denial("books", tool) is None, tool                           # and her read grants still work


def test_no_request_can_carry_the_app_into_the_principal(tmp_path, monkeypatch):
    init_local_profile(str(tmp_path / "perm"), user="alice")
    GrantStore(str(tmp_path / "perm")).add("app", "books", "tool", "books", actions=["read", "write", "delete"])
    pf = app_gate.install_gate("books")
    headers = [(b"x-app", b"books"), (b"x-user", b"alice"), (b"x-roles", b"admin"), (b"app", b"books"), (b"x-forwarded-app", b"books")]
    req = Request({"type": "http", "method": "POST", "path": "/agent/run", "query_string": b"app=books", "headers": headers})
    assert pf(req).app == "" and pf(None).app == ""
    for tool, action in TOOL_ACTIONS["books"].items():
        if action != "read":
            assert app_gate.tool_denial("books", tool) is not None


def test_an_unprovisioned_app_grant_with_no_local_profile_is_restricted_not_open(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", "k" * 32)
    GrantStore(str(tmp_path / "perm")).add("app", "books", "tool", "books", actions=["read", "write", "delete"])       # a store with only an app grant and no profile.json
    app_gate.install_gate("books")
    gate = app_gate.gate_for("books")
    assert gate.principal.user == "local" and gate.principal.app == ""                    # the documented default user; it holds no grant
    assert all(app_gate.tool_denial("books", t) is not None for t in TOOL_ACTIONS["books"])


def test_the_plane_itself_treats_an_app_grant_as_a_union_subject(tmp_path, monkeypatch):
    """For code that builds the identity itself and calls the plane directly (not the apps' gates): an app grant alone allows every identity carrying that app. Pinned as the documented semantics."""
    monkeypatch.setenv("PERMISSIONS_KEY", "k" * 32)
    store = GrantStore(str(tmp_path / "p"))
    store.add("app", "books", "tool", "books", actions=["read"])
    plane = PermissionsPlane(store, privileged_roles=())
    assert plane.authorize(Identity(app="books", user="", roles=[]), "tool", "books.close", "read").allowed            # app grant alone: allowed (no principal grant needed)
    assert plane.authorize(Identity(app="books", user="mallory", roles=[]), "tool", "books.close", "read").allowed
    assert not plane.authorize(Identity(app="billing", user="mallory", roles=[]), "tool", "books.close", "read").allowed
    assert not plane.authorize(Identity(app="books", user="x", roles=[]), "tool", "books.close", "write").allowed     # the action still has to be granted
    assert not plane.authorize(Identity(app="", user="mallory", roles=[]), "tool", "books.close", "read").allowed


def test_the_semantics_are_documented():
    import pathlib
    guide = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "PERMISSIONS_MIGRATION.md").read_text()
    assert "AGPL basic plane: app grants are union subjects; the principal is the local profile; fleet app/principal two-gate model is Enterprise" in guide
