"""DR-09: a missing ``profile.json`` still means the default local user ``local``; it is logged, documented, and never inferred from request headers."""
from __future__ import annotations

import logging
import pathlib

import pytest
from starlette.requests import Request

from agentic_os import app_gate
from agentic_os.permissions import GrantStore
from agentic_os.permissions_setup import DEFAULT_USER, PROFILE_FILE, init_local_profile, local_user

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "AGENTIC_OS_PERMISSIONS", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    app_gate.reset_state()
    yield
    app_gate.reset_state()


def _store_without_profile(tmp_path, monkeypatch, user=DEFAULT_USER):
    """A store provisioned before profiles existed: a key and a grant for ``user``, no profile.json."""
    monkeypatch.setenv("PERMISSIONS_KEY", "k" * 32)
    store = GrantStore(str(tmp_path / "perm"))
    store.add("user", user, "tool", "guide.guide.retrieve", actions=["read"])
    assert not (tmp_path / "perm" / PROFILE_FILE).exists()


def test_the_default_user_is_local_and_the_fallback_is_logged(tmp_path, monkeypatch, caplog):
    _store_without_profile(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert local_user(str(tmp_path / "perm")) == DEFAULT_USER == "local"
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "profile.json" in msg and "'local'" in msg and "permissions init" in msg


def test_installing_a_gate_without_a_profile_runs_as_local_and_says_so(tmp_path, monkeypatch, caplog):
    _store_without_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    with caplog.at_level(logging.WARNING):
        pf = app_gate.install_gate("guide")
    assert pf().user == "local" and any("default local user" in r.getMessage() for r in caplog.records)
    assert app_gate.tool_denial("guide", "guide.retrieve") is None                          # local holds the grant: the quickstart-era store keeps working


def test_no_request_header_or_query_can_choose_the_principal_when_the_profile_is_missing(tmp_path, monkeypatch):
    _store_without_profile(tmp_path, monkeypatch, user="local")
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    pf = app_gate.install_gate("guide")
    headers = [(b"x-user", b"root"), (b"x-roles", b"admin,policy-admin"), (b"authorization", b"Bearer admin"), (b"x-profile", b"root"), (b"x-forwarded-user", b"root"),
               (b"remote-user", b"root"), (b"x-test-user", b"root")]
    req = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"user=root&profile=root&roles=admin", "headers": headers})
    p = pf(req)
    assert p.user == "local" and p.roles == [] and p.app == ""
    assert pf(None).user == "local"


def test_an_attacker_cannot_become_a_user_that_holds_grants_by_naming_one(tmp_path, monkeypatch):
    _store_without_profile(tmp_path, monkeypatch, user="alice")                             # the grant belongs to alice; the default user is local
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    pf = app_gate.install_gate("guide")
    req = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"user=alice", "headers": [(b"x-user", b"alice")]})
    assert pf(req).user == "local" and app_gate.tool_denial("guide", "guide.retrieve") is not None


def test_a_present_profile_replaces_the_fallback_and_init_writes_it(tmp_path, monkeypatch):
    _store_without_profile(tmp_path, monkeypatch)
    init_local_profile(str(tmp_path / "perm"), user="alice")
    assert local_user(str(tmp_path / "perm")) == "alice"


def test_the_fallback_is_documented_in_the_quickstart_threat_model():
    guide = (ROOT / "docs" / "PERMISSIONS_MIGRATION.md").read_text()
    sec = (ROOT / "docs" / "SECURITY_MODEL.md").read_text()
    for text in (guide, sec):
        assert "profile.json" in text and "default local user" in text and "`local`" in text
