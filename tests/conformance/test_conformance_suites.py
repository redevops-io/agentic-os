"""Suite conformance (plan §3.1/§13): the six domain suites compose only runtime-native apps.

Builds an AppRegistry from the migrated app manifests (self-inserts apps/), registers the six
domain suites, and asserts each composes registered apps and surfaces a non-empty, real capability
union. Skips if the apps aren't importable here.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

from agentic_os.app_kit import AppRegistry, SuiteError, SuiteRegistry
from agentic_os.suites import DOMAIN_SUITES, register_domain_suites

_APPS_DIR = pathlib.Path(__file__).resolve().parents[2] / "apps"


def _app_registry():
    if not _APPS_DIR.is_dir():
        pytest.skip("apps/ not present")
    if str(_APPS_DIR) not in sys.path:
        sys.path.insert(0, str(_APPS_DIR))
    reg = AppRegistry()
    needed = sorted({app for suite in DOMAIN_SUITES for app in suite.apps})
    for app in needed:
        try:
            importlib.import_module(f"{app}.manifest").register(reg)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"{app} not importable here: {exc}")
    return reg


def test_all_six_domain_suites_register_conformantly():
    reg = register_domain_suites(_app_registry())
    assert reg.names() == sorted(s.name for s in DOMAIN_SUITES)
    # every suite surfaces a non-empty capability union drawn from its composed apps
    for suite in DOMAIN_SUITES:
        caps = reg.suite(suite.name).capabilities
        assert caps, f"{suite.name} surfaces no capabilities"


def test_suite_capability_union_matches_composed_apps():
    apps = _app_registry()
    reg = register_domain_suites(apps)
    rev = reg.suite("revenue")
    # revenue composes crm/outreach/market/growth-assistant -> its caps include each app's caps
    assert "crm.draft_outreach" in rev.capabilities
    assert "outreach.send_all" in rev.capabilities
    assert "radar.brief" in rev.capabilities


def test_suite_composing_an_unregistered_app_fails_closed():
    apps = _app_registry()
    from agentic_os.app_kit import Suite
    bad = Suite(name="bogus", apps=("does-not-exist",))
    sreg = SuiteRegistry(apps)
    with pytest.raises(SuiteError, match="not registered"):
        sreg.register(bad)


def test_empty_suite_is_rejected():
    from agentic_os.app_kit import Suite
    sreg = SuiteRegistry(_app_registry())
    with pytest.raises(SuiteError, match="no apps"):
        sreg.register(Suite(name="empty", apps=()))
