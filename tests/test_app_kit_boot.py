"""Boot-time registration: shared registry, idempotent, fail-closed."""
from __future__ import annotations

import types

import pytest

from agentic_os.app_kit import (
    AppManifest,
    CapabilityRef,
    ManifestError,
    VerifierRef,
    default_registry,
    register_app_manifest,
    reset_default_registry,
)
from agentic_os.app_kit.boot import register_app_manifest as _reg
from agentic_os.mission.operator_sdk import Operator, capability


def _manifest_module(name, *, conformant=True):
    op = Operator(name, [capability(f"{name}.read", lambda i: {"x": 1}, provides=["x"]),
                         capability(f"{name}.write", lambda i: {"done": 1}, provides=["done"],
                                    outputs={"done": "int"}, side_effecting=True)])
    verifiers = (VerifierRef(f"{name}.write", "composite:done"),) if conformant else ()
    manifest = AppManifest(name=name,
                           capabilities=(CapabilityRef(f"{name}.read"), CapabilityRef(f"{name}.write")),
                           verifiers=verifiers)
    mod = types.SimpleNamespace(MANIFEST=manifest, default_operator=lambda: op)
    return mod


def setup_function(_):
    reset_default_registry()


def test_registers_into_the_shared_default_registry():
    mod = _manifest_module("app1")
    app = register_app_manifest(mod)
    assert app.manifest.name == "app1"
    assert "app1" in default_registry()


def test_idempotent_across_repeated_boot_imports():
    mod = _manifest_module("app2")
    a = register_app_manifest(mod)
    b = register_app_manifest(mod)          # second import in the same process
    assert a is b                            # no duplicate-registration error
    assert default_registry().names() == ["app2"]


def test_fails_closed_on_non_conformant_app():
    mod = _manifest_module("bad", conformant=False)   # side-effecting cap, no verifier -> N4 fail
    with pytest.raises(ManifestError):
        register_app_manifest(mod)
    assert "bad" not in default_registry()


def test_isolated_registry_argument():
    from agentic_os.app_kit import AppRegistry
    reg = AppRegistry()
    _reg(_manifest_module("app3"), registry=reg)
    assert "app3" in reg
    assert "app3" not in default_registry()   # the explicit registry is used, not the default
