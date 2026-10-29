"""KD-1: the engine keeps its process-wide authorizer where IT keeps it. The real engine holds ``_default_authorizer`` in ``agent_harness.tools`` and only
re-exports ``set_default_authorizer`` from ``context_runtime.tools.base``; reading ``base._default_authorizer`` made every enterprise-mode start fail with
'bootstrap() returned but no process-wide authorizer is installed'. The lookup is layout-agnostic now and says so clearly when it cannot find the variable."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.app_gate import GateStartupError, install_gate  # noqa: E402

APP = "growth-assistant"
SPEC = type("S", (), {"name": "assistant.ask", "side_effecting": False})()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "AGENTIC_OS_PERMISSIONS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_PROCESS_AUTHORIZER", None)


@pytest.mark.parametrize("layout", ["base", "core", "getter"])
class TestEveryKnownLayout:
    def test_the_authorizer_the_enterprise_bootstrap_installs_is_found(self, monkeypatch, layout):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        s = stub.install(monkeypatch, fleet={}, layout=layout)
        pf = install_gate(APP)                                   # does NOT refuse: this is the KD-1 failure
        assert callable(pf) and s.default_authorizer() is not None
        assert app_gate.default_authorizer() is s.default_authorizer()
        assert app_gate.default_authorizer()(None, SPEC, {}) is not None            # anonymous: the gate answers (denies)

    def test_the_value_is_read_at_call_time(self, monkeypatch, layout):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        s = stub.install(monkeypatch, fleet={}, layout=layout)
        install_gate(APP)
        later = lambda p, spec, a: None                          # noqa: E731
        s.base.set_default_authorizer(later)
        assert app_gate.default_authorizer() is later
        s.base.set_default_authorizer(None)
        assert app_gate.default_authorizer() is None

    def test_a_bootstrap_that_installs_nothing_is_still_refused(self, monkeypatch, layout):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        s = stub.install(monkeypatch, fleet={}, layout=layout)
        s.modules["context_runtime_enterprise.apps"].bootstrap = lambda app, **kw: (lambda request=None: None)
        with pytest.raises(GateStartupError) as e:
            install_gate(APP)
        assert "no process-wide authorizer" in str(e.value)

    def test_basic_mode_installs_through_the_setter_whatever_the_layout(self, monkeypatch, tmp_path, layout):
        from agentic_os.permissions_setup import init_local_profile
        monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
        monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
        s = stub.install(monkeypatch, fleet={}, layout=layout)
        init_local_profile(str(tmp_path / "perm"))
        install_gate(APP)
        assert s.default_authorizer() is app_gate._PROCESS_AUTHORIZER is not None


def test_an_engine_whose_authorizer_cannot_be_read_back_is_a_clear_start_failure_in_enterprise_mode(monkeypatch):
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    stub.install(monkeypatch, fleet={}, layout="unknown")
    with pytest.raises(GateStartupError) as e:
        install_gate(APP)
    msg = str(e.value)
    assert "cannot read" in msg and "default authorizer" in msg and "get_default_authorizer" in msg           # says what it looked for


def test_an_unreadable_engine_is_fail_closed_at_call_time_never_open(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    stub.install(monkeypatch, fleet={}, layout="unknown")
    assert app_gate.default_authorizer() is None             # unknown, so None: a console that sees None while permissions are requested DENIES


def test_without_the_engine_nothing_is_found_and_nothing_raises(monkeypatch):
    stub.absent(monkeypatch)
    monkeypatch.setitem(sys.modules, "context_runtime", None)
    assert app_gate.default_authorizer() is None
