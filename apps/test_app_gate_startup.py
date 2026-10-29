"""RR-01, per app: with permissions requested an app starts gated or refuses to start; without the flag it starts as before.

Every one of the ten apps that join the enterprise permissions plane is IMPORTED here (that is what starting an app is: its module-level
``install_gate`` call runs). The enterprise bootstrap is a STAND-IN (``tests/_cr_enterprise_stub.py``) that raises exactly where the real
one does; ``context_runtime`` is not installable here. So this checks what each shipped app does with the bootstrap's start-up error.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os.app_gate import GateStartupError  # noqa: E402
from agentic_os.tool_actions import TOOL_ACTIONS  # noqa: E402

APPS = ("agentic-crm", "agentic-privacy", "billing", "books", "compliance", "control-tower", "document-editor", "edge-sentinel",
        "growth-assistant", "growth-engine", "guide", "infra", "lifecycle", "market-radar", "outreach-engine", "social-autopilot", "support")
KERNEL_CLASSIFIED = tuple(a for a in APPS if a in TOOL_ACTIONS)
ENTERPRISE_ONLY = tuple(a for a in APPS if a not in TOOL_ACTIONS)          # classified only by the enterprise fleet table
FLEET = {a: {"some_tool": "read", "some.write_tool": "write"} for a in ENTERPRISE_ONLY}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "CR_PERMISSIONS_DEV_UNGATED_ACK", "CR_IDENTITY_SECRET_FILE"):
        monkeypatch.delenv(k, raising=False)


def _start(monkeypatch, app: str):
    """Start the app: import its module afresh so its module-level gate wiring runs."""
    for name in [n for n in sys.modules if n == f"{app}.app" or n.startswith(f"{app}.app.")]:
        monkeypatch.delitem(sys.modules, name)
    return importlib.import_module(f"{app}.app")


def test_there_are_seventeen_bootstrapping_apps_and_the_kernel_classifies_every_one_of_them():
    assert len(APPS) == 17 and set(KERNEL_CLASSIFIED) == set(APPS) and ENTERPRISE_ONLY == ()


@pytest.mark.parametrize("app", APPS)
class TestEveryApp:
    def test_permissions_on_with_a_table_starts_gated_with_the_resolved_classification(self, monkeypatch, app):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        s = stub.install(monkeypatch, fleet=FLEET)
        _start(monkeypatch, app)
        call = [c for c in s.bootstrap_calls if c["app"] == app][-1]
        assert call["tool_actions"], call
        assert s.default_authorizer() is not None
        if app in TOOL_ACTIONS:
            assert set(TOOL_ACTIONS[app]) <= set(call["tool_actions"])

    @pytest.mark.parametrize("flag", ["1", "true", "yes", "on"])
    def test_permissions_on_and_the_enterprise_package_missing_refuses_to_start(self, monkeypatch, app, flag):
        monkeypatch.setenv("CR_PERMISSIONS", flag)
        stub.absent(monkeypatch)
        with pytest.raises(GateStartupError):
            _start(monkeypatch, app)

    def test_a_bad_identity_mode_refuses_to_start(self, monkeypatch, app):
        monkeypatch.setenv("CR_IDENTITY_MODE", "bogus")
        stub.install(monkeypatch, fleet=FLEET)
        with pytest.raises(GateStartupError) as e:
            _start(monkeypatch, app)
        assert "IdentityConfigError" in str(e.value)

    def test_permissions_on_and_a_bootstrap_that_installs_no_gate_refuses_to_start(self, monkeypatch, app):
        monkeypatch.setenv("CR_PERMISSIONS", "1")
        s = stub.install(monkeypatch, fleet=FLEET)
        s.modules["context_runtime_enterprise.apps"].bootstrap = lambda a, **kw: (lambda request=None: None)
        with pytest.raises(GateStartupError):
            _start(monkeypatch, app)

    def test_without_the_flag_the_app_starts_as_before_with_or_without_the_enterprise_package(self, monkeypatch, app):
        stub.absent(monkeypatch)
        assert _start(monkeypatch, app).app is not None
        stub.install(monkeypatch)
        assert _start(monkeypatch, app).app is not None

    def test_without_the_flag_a_failing_bootstrap_does_not_stop_the_app(self, monkeypatch, app):
        s = stub.install(monkeypatch)
        s.modules["context_runtime_enterprise.apps"].bootstrap = lambda a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        assert _start(monkeypatch, app).app is not None


@pytest.mark.parametrize("app", ENTERPRISE_ONLY)
def test_an_app_the_kernel_does_not_classify_refuses_to_start_when_the_enterprise_has_no_table_for_it(monkeypatch, app):
    """The exact RR-01 failure: CR_PERMISSIONS=1 and no tool_actions. It must stop the app, not be swallowed."""
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    stub.install(monkeypatch, fleet={})                     # an enterprise build without a table for this app
    with pytest.raises(GateStartupError) as e:
        _start(monkeypatch, app)
    assert "no tool classification table" in str(e.value)


@pytest.mark.parametrize("app", KERNEL_CLASSIFIED)
def test_a_kernel_classified_app_needs_no_enterprise_table_to_start_gated(monkeypatch, app):
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    s = stub.install(monkeypatch, fleet={})
    _start(monkeypatch, app)
    assert [c for c in s.bootstrap_calls if c["app"] == app][-1]["tool_actions"] == TOOL_ACTIONS[app]


@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("value", ["enabled", "y", "2", "True ,", "tru\u200be"])
def test_an_unrecognised_permissions_switch_value_stops_every_app_instead_of_starting_it_ungated(monkeypatch, app, value):
    """FR-03: the sweep found 17/17 apps starting with no gate and no warning on AGENTIC_OS_PERMISSIONS=enabled."""
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", value)
    stub.absent(monkeypatch)
    with pytest.raises(GateStartupError) as e:
        _start(monkeypatch, app)
    assert "not a recognised value" in str(e.value)
