"""F-1 of the final-tip review: the in-page purge confirmation ignores the confirming activation for a short delay after arming.
Static guard that runs without a browser (the behaviour is tested in tests/test_launcher_page_browser.py)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "deploy" / "launcher" / "ui" / "app.js").read_text(encoding="utf-8")


def test_the_purge_confirmation_has_a_minimum_delay_of_about_800_ms():
    m = re.search(r"const PURGE_CONFIRM_DELAY_MS\s*=\s*(\d+)\s*;", APP)
    assert m and 600 <= int(m.group(1)) <= 1200, "PURGE_CONFIRM_DELAY_MS must exist and be about 800"
    assert re.search(r"const PURGE_ARM_MS\s*=\s*8000\s*;", APP), "the arming time-out stays 8 s"


def test_the_waiting_state_uses_aria_disabled_never_the_disabled_attribute_and_ignores_key_repeat():
    assert "aria-disabled" in APP
    assert "purge.disabled" not in APP and 'setAttribute("disabled"' not in APP
    assert "ev.repeat" in APP, "auto-repeat of a held key must not confirm"


def test_the_purge_is_still_only_in_the_launcher_app_js_not_the_vendored_ui_bundle():
    assert "advanced_docker_purge" in APP
    assert "PURGE_CONFIRM_DELAY_MS" not in (ROOT / "deploy" / "launcher" / "ui" / "index.html").read_text(encoding="utf-8")
