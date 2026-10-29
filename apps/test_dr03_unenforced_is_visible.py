"""DR-03 / design decision: the unset default is accepted for this release, but an app that runs WITHOUT authorization says so everywhere a deployer looks.

* a conspicuous WARNING at start-up naming the exact configuration for an externally reachable deployment;
* ``authorization: unenforced`` (or ``restricted`` / ``enforced``) in the app's ``/health`` and in the operator's ``/_security``;
* documentation that never describes the deployment as protected by default, identifies the exact configuration, the ways an unset-and-reachable deployment can still run unenforced, and says that making the strict
  setting the default is planned for the next compatibility-breaking release (CHANGELOG "Planned").
"""
from __future__ import annotations

import importlib
import pathlib
import re
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _cr_enterprise_stub as stub  # noqa: E402
from _route_table import ROUTES  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.permissions_setup import init_local_profile  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
APPS = tuple(ROUTES)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "AGENTIC_OS_PERMISSIONS", "AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("SENTINEL_RATE_BURST", "1000000")
    monkeypatch.setenv("SENTINEL_RATE_PER_MIN", "1000000000")
    app_gate.reset_state()
    stub.absent(monkeypatch)
    yield
    app_gate.reset_state()


def start(monkeypatch, app):
    for name in [n for n in sys.modules if n == f"{app}.app"]:
        monkeypatch.delitem(sys.modules, name)
    return importlib.import_module(f"{app}.app")


# ── the warning ──

def test_the_unset_warning_names_the_exact_configuration_for_a_reachable_deployment(caplog):
    with caplog.at_level("WARNING", logger="agentic_os.app_gate"):
        app_gate.install_gate("guide")
    rec = [r for r in caplog.records if r.levelname == "WARNING"]
    assert rec, "no WARNING was logged"
    msg = " ".join(r.getMessage() for r in rec)
    for needle in ("AUTHORIZATION IS NOT ENFORCED", "AGENTIC_OS_PERMISSIONS=1", "agentic-os permissions init", "AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING=1", "AGENTIC_OS_PERMISSIONS=off",
                   "reachable", "authorization: unenforced"):
        assert needle in msg, needle


def test_a_deliberate_off_is_also_a_warning_not_a_whisper(monkeypatch, caplog):
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "off")
    with caplog.at_level("WARNING", logger="agentic_os.app_gate"):
        app_gate.install_gate("guide")
    msg = " ".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "DISABLED" in msg and "AGENTIC_OS_PERMISSIONS=off" in msg and "standalone" in msg


def test_enforcing_modes_log_no_unenforced_warning(monkeypatch, tmp_path, caplog):
    init_local_profile(str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    with caplog.at_level("WARNING", logger="agentic_os.app_gate"):
        app_gate.install_gate("guide")
    assert not any("NOT ENFORCED" in r.getMessage() or "DISABLED" in r.getMessage() for r in caplog.records)


# ── the status ──

def test_authorization_fields_for_every_mode(monkeypatch, tmp_path):
    app_gate.install_gate("guide")
    assert app_gate.authorization_fields("guide") == {"authorization": "unenforced", "authorization_setting": "unset"}
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "off")
    app_gate.install_gate("guide")
    assert app_gate.authorization_fields("guide") == {"authorization": "unenforced", "authorization_setting": "off"}
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    app_gate.install_gate("guide")
    assert app_gate.authorization_fields("guide") == {"authorization": "restricted", "authorization_setting": "on"}
    init_local_profile(str(tmp_path / "perm"))
    app_gate.install_gate("guide")
    assert app_gate.authorization_fields("guide") == {"authorization": "enforced", "authorization_setting": "on"}


def test_an_app_that_never_installed_a_gate_while_permissions_are_requested_reports_restricted(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    assert app_gate.authorization_fields("nobody")["authorization"] == "restricted"


@pytest.mark.parametrize("app", APPS)
def test_every_app_health_reports_the_authorization_state(monkeypatch, tmp_path, app):
    expected = {}
    c = TestClient(start(monkeypatch, app).app, raise_server_exceptions=False)
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["authorization"] == "unenforced" and r.json()["authorization_setting"] == "unset", (app, r.text[:200])
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "off")
    assert TestClient(start(monkeypatch, app).app, raise_server_exceptions=False).get("/health").json()["authorization_setting"] == "off"
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    assert TestClient(start(monkeypatch, app).app, raise_server_exceptions=False).get("/health").json()["authorization"] == "restricted"
    init_local_profile(str(tmp_path / "perm"))
    assert TestClient(start(monkeypatch, app).app, raise_server_exceptions=False).get("/health").json()["authorization"] == "enforced"
    del expected


def test_the_operators_security_status_reports_it_too(monkeypatch):
    from fastapi import FastAPI
    from agentic_os.mission.operator_sdk import Operator, capability
    op = Operator("books", [capability("books.x", lambda i: {})])
    app = FastAPI()
    app.include_router(op.router())
    assert TestClient(app).get("/_security").json()["authorization"] == "unenforced"
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    assert TestClient(app).get("/_security").json()["authorization"] == "restricted"


# ── the documentation ──

def _docs():
    out = {}
    for p in [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "apps" / "README.md", *sorted((ROOT / "docs").glob("*.md")), *sorted((ROOT / "apps").glob("*/README.md"))]:
        if p.exists():
            out[str(p.relative_to(ROOT))] = p.read_text()
    return out


def test_no_document_claims_the_deployment_is_protected_by_default():
    bad = [(n, m.group(0)) for n, t in _docs().items() for m in re.finditer(r"(?i)secure[ -]by[ -]default|secure defaults", t)]
    assert bad == []


def test_the_release_statement_is_in_the_migration_guide_and_the_changelog():
    guide, log = _docs()["docs/PERMISSIONS_MIGRATION.md"], _docs()["CHANGELOG.md"]
    stmt = "The permissions plane and the app execution paths were reviewed in a multi-stage, AI-assisted security review with separate reviewer agents working from the code diff; this is not an external audit or third-party certification. Deployment configuration may still run unenforced when the setting is absent."
    assert stmt in guide and stmt[0].lower() + stmt[1:] in log           # the CHANGELOG continues a sentence ("Until then: the permissions plane ...")
    assert "## Planned" in log and "strict permission setting" in log.split("## Planned", 1)[1] and "next compatibility-breaking release" in log.split("## Planned", 1)[1]


def test_the_guide_lists_the_ways_an_unset_and_reachable_deployment_can_still_run_unenforced():
    guide = _docs()["docs/PERMISSIONS_MIGRATION.md"]
    section = guide.split("### Ways an unset-and-reachable deployment can still run unenforced", 1)[1]
    for needle in ("uvicorn", "--host 0.0.0.0", "`infra`", "no image", "derived image", "document-editor", "guide", "outreach-engine", "fleet", "compose", "authorization: unenforced",
                   "AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING=1", "${VAR:-}"):
        assert needle in section, needle
