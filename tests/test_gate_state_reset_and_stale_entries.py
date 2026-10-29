"""The kernel's process-wide gate state (``_GATES``, ``_ENTERPRISE``, ``_NOTICED``, ``_PROCESS_AUTHORIZER``) outlives an import.

* ``app_gate.reset_state()`` is the one supported way to clear it (tests; a harness that re-imports apps), and the test fixtures use it.
* Production code cannot be confused by a stale entry: reinstalling an app starts from a clean entry for that app, a reinstall that no longer enforces basic permissions drops the stale process-wide
  authorizer, and if an app name were ever in BOTH registries the stricter answer wins (a stale basic entry cannot loosen an enterprise gate, nor the reverse).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentic_os import app_gate
from agentic_os.permissions_setup import init_local_profile


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PERMISSIONS", "CR_PERMISSIONS", "CR_IDENTITY_MODE", "AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    app_gate.reset_state()
    yield
    app_gate.reset_state()


def test_reset_state_clears_every_registry():
    app_gate._GATES["x"] = object()
    app_gate._ENTERPRISE["x"] = (lambda r: None, {})
    app_gate._NOTICED.add("x")
    app_gate._PROCESS_AUTHORIZER = lambda *a: None
    app_gate.reset_state()
    assert app_gate._GATES == {} and app_gate._ENTERPRISE == {} and app_gate._NOTICED == set() and app_gate._PROCESS_AUTHORIZER is None


def test_reset_state_keeps_the_same_registry_objects_so_importers_see_the_reset():
    gates, ent, noticed = app_gate._GATES, app_gate._ENTERPRISE, app_gate._NOTICED
    gates["x"] = 1
    app_gate.reset_state()
    assert app_gate._GATES is gates and app_gate._ENTERPRISE is ent and app_gate._NOTICED is noticed and gates == {}


def test_the_unconfigured_notice_is_given_again_after_a_reset(caplog):
    with caplog.at_level("WARNING"):
        app_gate.install_gate("guide")
        app_gate.install_gate("guide")
    assert sum("NOT enforced" in r.message or "not enforced" in r.message.lower() for r in caplog.records) == 1
    caplog.clear()
    app_gate.reset_state()
    with caplog.at_level("WARNING"):
        app_gate.install_gate("guide")
    assert any("not enforced" in r.message.lower() for r in caplog.records)


def test_reinstalling_an_app_without_basic_permissions_drops_its_stale_gate_and_the_stale_process_authorizer(monkeypatch, tmp_path):
    init_local_profile(str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    app_gate.install_gate("guide")
    assert app_gate.gate_for("guide") is not None and app_gate._PROCESS_AUTHORIZER is not None
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "off")                           # the same process, a new configuration: deliberately standalone
    app_gate.install_gate("guide")
    assert app_gate.gate_for("guide") is None and app_gate._PROCESS_AUTHORIZER is None
    assert app_gate.tool_denial("guide", "guide.retrieve") is None                # no stale gate decides for an app that is no longer gated


def test_a_process_authorizer_still_needed_by_another_gated_app_is_kept(monkeypatch, tmp_path):
    init_local_profile(str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    app_gate.install_gate("guide")
    app_gate.install_gate("support")
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "off")
    app_gate.install_gate("guide")
    assert app_gate.gate_for("support") is not None and app_gate._PROCESS_AUTHORIZER is not None


def test_reinstalling_in_another_mode_leaves_no_entry_of_the_old_mode(monkeypatch, tmp_path):
    init_local_profile(str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    app_gate.install_gate("guide")
    app_gate._ENTERPRISE["guide"] = (lambda r: None, {})                          # a stale enterprise entry from an earlier import
    app_gate.install_gate("guide")
    assert app_gate.gate_for("guide") is not None and "guide" not in app_gate._ENTERPRISE


def test_when_an_app_is_in_both_registries_the_stricter_answer_wins(monkeypatch):
    allow = SimpleNamespace(check=lambda tool, principal=None: None)
    deny = SimpleNamespace(check=lambda tool, principal=None: "basic says no")
    ent_deny = lambda principal, spec, ctx: "enterprise says no"   # noqa: E731
    ent_allow = lambda principal, spec, ctx: None                   # noqa: E731
    monkeypatch.setattr(app_gate, "_cr_default_authorizer", lambda: ent_deny)
    app_gate._ENTERPRISE["both"] = (lambda r: SimpleNamespace(user="u", roles=[]), {"both.t": "read"})
    app_gate._GATES["both"] = allow
    assert app_gate.tool_denial("both", "both.t") == "enterprise says no"        # a stale permissive basic entry cannot loosen the enterprise gate
    assert app_gate.operator_denial("both", "both.t") == "enterprise says no"
    app_gate._GATES["both"] = deny
    monkeypatch.setattr(app_gate, "_cr_default_authorizer", lambda: ent_allow)
    assert app_gate.tool_denial("both", "both.t") == "basic says no"             # nor the reverse
    assert app_gate.operator_denial("both", "both.t") == "basic says no"
    app_gate._GATES["both"] = allow
    assert app_gate.tool_denial("both", "both.t") is None


def test_the_test_fixtures_start_every_test_clean():
    assert app_gate._GATES == {} and app_gate._ENTERPRISE == {} and app_gate._NOTICED == set() and app_gate._PROCESS_AUTHORIZER is None
