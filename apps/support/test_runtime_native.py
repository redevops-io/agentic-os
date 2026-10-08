"""support runtime-native: registers a conformant AppManifest.

Run: PYTHONPATH=apps python -m pytest apps/support/test_runtime_native.py
"""
from __future__ import annotations

import importlib

from agentic_os.app_kit import AppRegistry

_manifest = importlib.import_module("support.manifest")


def test_registers_conformantly():
    reg = AppRegistry()
    app = _manifest.register(reg)                 # fails closed if non-conformant
    assert app.manifest.name == "support"
    assert app.manifest.capability_names()        # declares at least one capability
    # N4: every side-effecting capability is backed by a verifier (register enforced this)
    assert app.manifest.verified_capabilities() <= app.manifest.capability_names()


def test_manifest_constant_matches_registration():
    assert _manifest.MANIFEST.name == "support"
    assert _manifest.MANIFEST.required_cores
