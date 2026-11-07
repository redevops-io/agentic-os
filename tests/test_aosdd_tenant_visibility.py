"""AOS-DD tenant visibility in the Projects drill-down (a WARNING, never an error).

The drill-down hides a mission recorded under another tenant, and the local-owner principal that the desktop's launch session maps to carries the tenant
``agentic_os.overlays.DEFAULT_TENANT`` (``default``). An AOS-DD mission is recorded under the config's ``tenant``. If the two differ the mission is real but
the drill-down answers "No recorded history to show". The drill-down rule is unchanged; AOS-DD says so when it starts and when ``verify`` is given its config."""
from __future__ import annotations

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from aosdd_fakes import make_config  # noqa: E402
from test_aosdd_core import _files  # noqa: E402
from test_aosdd_runtime import Env  # noqa: E402

from agentic_os.integrations.aosdd import cli
from agentic_os.integrations.aosdd.config import AosddConfig, LOCAL_OWNER_TENANT
from agentic_os.overlays import DEFAULT_TENANT


def test_the_documented_local_owner_tenant_is_the_drilldowns_default_tenant():
    assert LOCAL_OWNER_TENANT == DEFAULT_TENANT == "default"


def test_a_config_tenant_other_than_the_local_owners_has_a_warning_naming_the_keys():
    w = make_config().tenant_warnings()                                       # the test config uses "aosdd-test"
    assert len(w) == 1 and "aosdd-test" in w[0] and "'tenant'" in w[0] and "default" in w[0] and "PROJECTS_DRILLDOWN_" in w[0] and "evidence" in w[0]
    assert make_config(tenant="default").tenant_warnings() == []


def test_starting_the_services_logs_the_warning_and_does_not_refuse(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="agentic_os.integrations.aosdd"):
        env = Env(tmp_path)
        env.runner()                                                           # builds Services: starts normally
    assert any("drill-down" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agentic_os.integrations.aosdd"):
        (tmp_path / "d").mkdir()
        Env(tmp_path / "d", cfg=make_config(tenant="default")).runner()
    assert not [r for r in caplog.records if "drill-down" in r.getMessage()]


def _write(tmp_path, tenant):
    return _files(tmp_path, tenant=tenant)[0]


def test_verify_with_a_config_reports_the_warning_and_stays_ok(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    c = _write(tmp_path, "aosdd-local")
    assert cli.main(["--state", str(state), "--config", c, "verify"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and len(out["warnings"]) == 1 and "aosdd-local" in out["warnings"][0]


def test_verify_with_the_local_owners_tenant_or_without_a_config_has_no_warning(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    c = _write(tmp_path, "default")
    assert cli.main(["--state", str(state), "--config", c, "verify"]) == 0
    assert json.loads(capsys.readouterr().out)["warnings"] == []
    assert cli.main(["--state", str(state), "verify"]) == 0
    assert "warnings" not in json.loads(capsys.readouterr().out)


def test_the_cli_prints_the_warning_on_stderr_when_it_starts_and_still_runs(tmp_path, capsys):
    from types import SimpleNamespace
    state = tmp_path / "state"
    c = _write(tmp_path, "aosdd-local")
    r, cfg = cli.build(SimpleNamespace(config=c, state=str(state), hold_after_response=None, hold_before_request=None, pause_after=None, grant_ttl=900.0))
    err = capsys.readouterr().err
    assert err.startswith("WARNING: config key 'tenant'") and r is not None and cfg.tenant == "aosdd-local"
    (tmp_path / "ok").mkdir()
    c2 = _write(tmp_path / "ok", "default")
    cli.build(SimpleNamespace(config=c2, state=str(tmp_path / "state2"), hold_after_response=None, hold_before_request=None, pause_after=None, grant_ttl=900.0))
    assert "WARNING" not in capsys.readouterr().err
