"""FR-10: when BOTH the engine's default authorizer and the kernel's own (basic mode) are installed, a call is allowed only if BOTH allow (the stricter rule): a permissive default
installed after start-up can never loosen basic mode, and basic mode never loosens an enterprise gate. One of them alone is returned as is."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.permissions import Identity  # noqa: E402
from agentic_os.permissions_setup import init_local_profile  # noqa: E402

SPEC = type("S", (), {"name": "assistant.ask", "side_effecting": False})()
WRITE = type("S", (), {"name": "assistant.playbook", "side_effecting": True})()
ALLOW = lambda p, s, a: None                    # noqa: E731
DENY = lambda p, s, a: "engine says no"         # noqa: E731


@pytest.fixture()
def basic(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_PROCESS_AUTHORIZER", None)
    s = stub.install(monkeypatch, fleet={})
    init_local_profile(str(tmp_path / "perm"))                       # read grants only
    app_gate.install_gate("growth-assistant")
    return s


def test_a_permissive_engine_default_installed_after_start_up_cannot_loosen_basic_mode(basic):
    basic.base.set_default_authorizer(ALLOW)                          # e.g. the enterprise dev acknowledgement, or anything calling set_default_authorizer
    auth = app_gate.default_authorizer()
    assert auth(Identity(user="local"), SPEC, {}) is None            # both allow a granted read
    assert auth(Identity(user="local"), WRITE, {}) is not None       # the kernel's gate still denies the write
    assert auth(Identity(user="stranger"), SPEC, {}) is not None


def test_a_strict_engine_default_also_binds_when_the_kernel_would_allow(basic):
    basic.base.set_default_authorizer(DENY)
    auth = app_gate.default_authorizer()
    assert auth(Identity(user="local"), SPEC, {}) == "engine says no"


def test_each_one_alone_is_returned_unchanged(monkeypatch, basic):
    kernel = app_gate._PROCESS_AUTHORIZER
    basic.base.set_default_authorizer(None)
    assert app_gate.default_authorizer() is kernel
    monkeypatch.setattr(app_gate, "_PROCESS_AUTHORIZER", None)
    basic.base.set_default_authorizer(ALLOW)
    assert app_gate.default_authorizer() is ALLOW
    basic.base.set_default_authorizer(None)
    assert app_gate.default_authorizer() is None


def test_the_same_function_on_both_sides_is_not_wrapped(basic):
    assert app_gate.default_authorizer() is app_gate._PROCESS_AUTHORIZER          # basic mode installs ONE authorizer in both places


def test_the_precedence_is_documented():
    t = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "PERMISSIONS_MIGRATION.md").read_text()
    assert "stricter" in t.lower() and "both allow" in t.lower()
