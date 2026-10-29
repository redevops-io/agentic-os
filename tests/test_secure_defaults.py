"""Design decision 4: an unconfigured installation is not consent to run without authorization.

States (``AGENTIC_OS_PERMISSIONS``): ``1`` = permissions enforced; ``off`` (any explicit off spelling) = deliberate standalone mode, logged as such; UNSET = unconfigured: the documented single-user quickstart keeps
working (basic default) but says so loudly at start-up and how to turn enforcement on; ``AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING=1`` turns "unset" into a start failure (the shipped Dockerfiles set it, so a
container that is reachable by design never starts on an unset setting); an unrecognised value always fails. The loopback/non-loopback rule is NOT implementable robustly at import time (the bind address
belongs to the process manager: containers bind 0.0.0.0 by design): where the code itself binds (an app's ``__main__``) the host is checked, everywhere else the REQUIRE switch is the mechanism.
"""
from __future__ import annotations

import pathlib

import pytest

import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.app_gate import GateStartupError, install_gate  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = "growth-assistant"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "AGENTIC_OS_PERMISSIONS", "AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setattr(app_gate, "_NOTICED", set())
    stub.absent(monkeypatch)


def test_unset_keeps_the_quickstart_working_and_says_loudly_that_authorization_is_not_enforced(caplog):
    with caplog.at_level("WARNING", logger="agentic_os.app_gate"):
        pf = install_gate(APP)
    assert app_gate.gate_for(APP) is None and callable(pf)
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "NOT ENFORCED" in msg and "AGENTIC_OS_PERMISSIONS=1" in msg and "agentic-os permissions init" in msg and "AGENTIC_OS_PERMISSIONS=off" in msg


def test_the_notice_is_once_per_app_not_a_flood(caplog):
    with caplog.at_level("WARNING", logger="agentic_os.app_gate"):
        install_gate(APP)
        install_gate(APP)
        install_gate("support")
    assert sum("NOT ENFORCED" in r.getMessage() for r in caplog.records) == 2


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "OFF"])
def test_an_explicit_off_is_the_documented_standalone_mode_with_a_banner(monkeypatch, caplog, value):
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", value)
    with caplog.at_level("INFO", logger="agentic_os.app_gate"):
        install_gate(APP)
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "explicitly disabled" in msg and "standalone" in msg and "NOT ENFORCED" not in msg


def test_with_the_require_switch_an_unset_setting_refuses_to_start_naming_the_fix(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING", "1")
    with pytest.raises(GateStartupError) as e:
        install_gate(APP)
    m = str(e.value)
    assert "AGENTIC_OS_PERMISSIONS=1" in m and "AGENTIC_OS_PERMISSIONS=off" in m and "agentic-os permissions init" in m and "refuses to start" in m


@pytest.mark.parametrize("value", ["off", "1"])
def test_with_the_require_switch_an_explicit_setting_starts(monkeypatch, tmp_path, value):
    monkeypatch.setenv("AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING", "1")
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", value)
    assert callable(install_gate(APP))


def test_the_require_switch_accepts_an_enterprise_request_as_an_explicit_setting(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING", "1")
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    with pytest.raises(app_gate.EnterpriseCapabilityMissing):                  # explicit, so it is not the 'unset' failure; the missing package is
        install_gate(APP)


def test_an_unrecognised_require_value_fails_too(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING", "maybe")
    with pytest.raises(GateStartupError):
        install_gate(APP)


def test_the_documented_single_user_quickstart_still_works_with_the_require_switch_on(monkeypatch, tmp_path):
    """agentic-os permissions init -> export PERMISSIONS_DIR=... AGENTIC_OS_PERMISSIONS=1 -> the app starts gated; reads work, writes need --include-write."""
    from agentic_os.permissions_setup import init_local_profile
    init_local_profile(str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING", "1")
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    gate = install_gate(APP).gate
    assert gate.check("assistant.ask") is None and gate.check("assistant.playbook") is not None


class TestExposedBind:
    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "example.org"])
    def test_an_app_that_binds_a_reachable_address_itself_refuses_when_nothing_is_set(self, host):
        with pytest.raises(SystemExit) as e:
            app_gate.require_explicit_setting_when_exposed(host)
        assert "AGENTIC_OS_PERMISSIONS" in str(e.value)

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_loopback_is_the_local_dev_path_and_is_not_refused(self, host):
        app_gate.require_explicit_setting_when_exposed(host)

    @pytest.mark.parametrize("setting", [{"AGENTIC_OS_PERMISSIONS": "off"}, {"AGENTIC_OS_PERMISSIONS": "1"}, {"CR_PERMISSIONS": "1"}])
    def test_an_explicit_setting_allows_it(self, monkeypatch, setting):
        for k, v in setting.items():
            monkeypatch.setenv(k, v)
        app_gate.require_explicit_setting_when_exposed("0.0.0.0")


def test_every_shipped_app_image_requires_the_explicit_setting():
    docker = sorted((ROOT / "apps").glob("*/Dockerfile"))
    assert len(docker) >= 17
    for f in docker:
        assert "AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING=1" in f.read_text(), f


def test_every_app_that_binds_a_port_in_its_main_checks_the_host():
    for f in sorted((ROOT / "apps").glob("*/app.py")):
        t = f.read_text()
        if 'uvicorn.run(app, host="0.0.0.0"' in t:
            assert "_exposed" in t.split('uvicorn.run(app, host="0.0.0.0"')[0].rsplit("__main__", 1)[-1], f


def test_every_documented_docker_run_in_the_app_readmes_sets_the_permissions_mode_explicitly():
    import re
    for f in sorted((ROOT / "apps").glob("*/README.md")) + [ROOT / "apps" / "README.md"]:
        text = f.read_text()
        for m in re.finditer(r"^docker run[^\n]*(?:\\\n[^\n]*)*", text, flags=re.M):
            assert "AGENTIC_OS_PERMISSIONS" in m.group(0), (f, m.group(0)[:80])


def test_the_rule_and_the_impact_table_are_documented():
    t = (ROOT / "docs" / "PERMISSIONS_MIGRATION.md").read_text()
    for needle in ("AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING", "unconfigured", "loopback", "impact", "standalone"):
        assert needle.lower() in t.lower(), needle
