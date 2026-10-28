"""KD-2: with nothing requested the kernel must behave the same with the enterprise package present or absent (the documented basic default).

It used to call the enterprise bootstrap with its install default, which installs a deny-all gate when permissions are off: package present = every console tool denied,
package absent = basic default."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.app_gate import install_gate  # noqa: E402

APP = "growth-assistant"
SPEC = type("S", (), {"name": "assistant.ask", "side_effecting": False})()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "AGENTIC_OS_PERMISSIONS", "CR_PERMISSIONS_DEV_UNGATED_ACK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_PROCESS_AUTHORIZER", None)


@pytest.mark.parametrize("layout", ["base", "core", "getter"])
def test_nothing_requested_installs_no_gate_with_the_package_present(monkeypatch, layout):
    s = stub.install(monkeypatch, fleet={}, layout=layout)
    install_gate(APP)
    assert s.bootstrap_calls == []                      # DR-02 (design decision): with nothing requested the enterprise package is not imported or called at all
    assert s.default_authorizer() is None and app_gate.default_authorizer() is None


def test_present_and_absent_are_the_same_when_nothing_is_requested(monkeypatch):
    stub.absent(monkeypatch)
    install_gate(APP)
    absent = (app_gate.default_authorizer(), app_gate.permissions_requested(), app_gate.gate_for(APP))
    s = stub.install(monkeypatch, fleet={})
    install_gate(APP)
    present = (app_gate.default_authorizer(), app_gate.permissions_requested(), app_gate.gate_for(APP))
    assert absent == present == (None, False, None)
    assert s.default_authorizer() is None


def test_the_bootstrap_is_not_called_for_the_identity_either_when_nothing_is_requested(monkeypatch):
    """KD-2 first resolved the identity through the bootstrap with install=False; the DR-02 decision goes further: nothing requested = the package is never imported."""
    s = stub.install(monkeypatch, fleet={})
    principal_for = install_gate(APP)
    assert principal_for is install_gate.__globals__["_ungated"] and s.bootstrap_calls == []


def test_a_bootstrap_that_cannot_be_told_not_to_install_is_not_called_when_nothing_is_requested(monkeypatch):
    """An older enterprise build has no ``install`` argument, so its default (a deny-all gate) cannot be switched off: with nothing requested it is simply not
    called, which is the basic default."""
    s = stub.install(monkeypatch, fleet={}, bootstrap_has_install=False)
    pf = install_gate(APP)
    assert s.bootstrap_calls == [] and pf(None) is None and s.default_authorizer() is None      # (and no bootstrap is called whatever its signature)


def test_requested_permissions_still_install_the_gate(monkeypatch):
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    s = stub.install(monkeypatch, fleet={})
    install_gate(APP)
    assert s.bootstrap_calls[-1]["install"] is True and s.default_authorizer() is not None


def test_the_documented_default_says_the_same_with_or_without_the_package():
    t = (pathlib.Path(__file__).resolve().parents[1] / "agentic_os" / "app_gate.py").read_text()
    assert "unchanged (the enterprise bootstrap runs)" not in t and "never imports" in t
