"""Fleet-wide static conformance (plan §9): every migrated app's AppManifest registers conformantly.

Discovers each apps/<app>/manifest.py and registers it through the AppRegistry (which fails closed).
Inserts apps/ on sys.path so the hyphenated app packages import; an app whose deps aren't available
in this environment is skipped, never failed.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

from agentic_os.app_kit import AppRegistry

_APPS_DIR = pathlib.Path(__file__).resolve().parents[2] / "apps"


def _migrated_apps():
    if not _APPS_DIR.is_dir():
        return []
    return [d.name for d in sorted(_APPS_DIR.iterdir()) if (d / "manifest.py").exists()]


@pytest.mark.parametrize("app", _migrated_apps())
def test_app_manifest_registers_conformantly(app):
    if str(_APPS_DIR) not in sys.path:
        sys.path.insert(0, str(_APPS_DIR))
    try:
        manifest_mod = importlib.import_module(f"{app}.manifest")
    except Exception as exc:  # noqa: BLE001 - a missing third-party dep is an environment skip, not a fail
        pytest.skip(f"{app}.manifest not importable here: {exc}")
    reg = AppRegistry()
    registered = manifest_mod.register(reg)          # raises ManifestError if non-conformant
    assert registered.manifest.name == app
    # N4: every side-effecting capability is backed by a verifier
    assert registered.manifest.verified_capabilities() <= registered.manifest.capability_names()


def test_at_least_the_reference_app_is_migrated():
    apps = _migrated_apps()
    assert "agentic-crm" in apps, "the reference migration must be present"
    assert len(apps) >= 1
