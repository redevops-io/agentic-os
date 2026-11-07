"""Shared fixtures for app tests.

`load_app` imports `<pkg>.app` (the FastAPI module). Some apps import `context_runtime` unguarded at module top,
and that proprietary package is not installed in CI/dev; when it is absent a minimal stub providing only the two
names the apps import at top level is injected for the test (never for production, never if the real one exists).
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest


def _stub_context_runtime(monkeypatch) -> None:
    try:
        import context_runtime  # noqa: F401
        return
    except ImportError:
        pass
    pkg = types.ModuleType("context_runtime"); pkg.__path__ = []
    adapters = types.ModuleType("context_runtime.adapters"); adapters.__path__ = []
    mo = types.ModuleType("context_runtime.adapters.model_openai")
    mo.OpenAICompatibleModel = type("OpenAICompatibleModel", (), {"from_env": classmethod(lambda cls: None)})
    ty = types.ModuleType("context_runtime.types")
    ty.ModelRequest = type("ModelRequest", (), {"__init__": lambda self, *a, **k: None})
    for name, mod in (("context_runtime", pkg), ("context_runtime.adapters", adapters),
                      ("context_runtime.adapters.model_openai", mo), ("context_runtime.types", ty)):
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture()
def load_app(monkeypatch):
    def _load(pkg: str):
        _stub_context_runtime(monkeypatch)
        return importlib.import_module(f"{pkg}.app")
    return _load


@pytest.fixture(autouse=True)
def _explicit_test_authority_policy(monkeypatch):
    """Side effects are refused unless the owner's envelope covers them or the runtime was given an explicit, named test policy
    (agentic_os/mission/authority_policy.py); there is no silent "development allows" default. The apps' tests build runtimes
    without an envelope, so they declare ONE named policy here, recorded in every mission log they touch (``TestPolicyActive``).
    It is the app-suite counterpart of the one in tests/conftest.py. Tests of the fail-closed behaviour use ``no_test_policy``."""
    from agentic_os.mission.authority_policy import AuthorityTestPolicy
    from agentic_os.mission.runtime import MissionRuntime
    monkeypatch.setattr(MissionRuntime, "test_policy", AuthorityTestPolicy.allow_all(
        "pytest-apps-default", "the apps' tests build runtimes without owner envelopes; this named policy is their declaration"))
    yield


@pytest.fixture
def no_test_policy(monkeypatch):
    """Remove the suite's default test policy: the runtime then behaves as an unconfigured deployment (fail closed)."""
    from agentic_os.mission.runtime import MissionRuntime
    monkeypatch.setattr(MissionRuntime, "test_policy", None)
    yield


@pytest.fixture(autouse=True)
def _operator_invoke_open_for_legacy_tests(monkeypatch):
    """An operator's /invoke runs a side-effecting capability only for an authenticated identity with current authority and the applicable
    approval (agentic_os/mission/invoke_gate.py); there is no "development allows" default. The apps' handler-level tests call /invoke
    without envelopes, so they declare ONE explicit, named test policy here (the credential-less caller is accepted as its named test
    identity, and the mandatory approval is waived because those tests drive the handlers' own staging). Auth tests clear it explicitly."""
    from agentic_os.mission.authority_policy import AuthorityTestPolicy
    from agentic_os.mission.operator_sdk import Operator
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
    monkeypatch.setattr(Operator, "test_policy", AuthorityTestPolicy.allow_all(
        "pytest-apps-operator-default", "the apps' handler-level tests call /invoke without owner envelopes; this named policy is its declaration",
        anonymous_operators=True, unapproved=True))
    yield


@pytest.fixture(autouse=True)
def _app_gate_state_is_per_test(monkeypatch):
    """The gate registries are process-global (an app registers its gate at import). A test that installs one must not leak it into the next test."""
    from agentic_os import app_gate
    app_gate.reset_state()
    yield
    app_gate.reset_state()
