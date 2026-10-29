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
def _operator_invoke_open_for_legacy_tests(monkeypatch):
    """Operator /invoke fails closed without a credential; pre-existing handler tests exercise it directly, so
    they opt into the documented dev flag. Auth tests clear it explicitly."""
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
    # F21: `approval_required` is enforced on every /invoke path in every profile. These handler-level tests drive the
    # handlers directly (including their own staging), so they opt into the explicit, non-production testing override.
    # The default behaviour has its own tests (tests/test_invoke_approval_required.py) which clear it.
    monkeypatch.setenv("AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE", "1")
    yield


@pytest.fixture(autouse=True)
def _app_gate_state_is_per_test(monkeypatch):
    """The gate registries are process-global (an app registers its gate at import). A test that installs one must not leak it into the next test."""
    from agentic_os import app_gate
    app_gate.reset_state()
    yield
    app_gate.reset_state()
