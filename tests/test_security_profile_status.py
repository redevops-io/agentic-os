"""The effective security profile is visible: in startup logs, readiness checks and Edge Sentinel input."""
from __future__ import annotations

import logging

from agentic_os.mission import profile as pr
from agentic_os.mission.authority import AuthorityGuard, MemoryEnvelopeStore, MemoryLedger


def test_unset_profile_is_reported_as_defaulted_development(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_PROFILE", raising=False)
    monkeypatch.delenv("AGENTIC_OS_REQUIRE_AUTHORITY", raising=False)
    st = pr.status()
    assert st["profile"] == "development" and st["explicit"] is False and st["ok"] is False
    assert any("not set" in w for w in st["warnings"]) and any("not enforced" in w for w in st["warnings"])


def test_production_with_a_non_durable_guard_is_not_ok(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    st = pr.status(AuthorityGuard(MemoryEnvelopeStore(), MemoryLedger()), replay_durable=False)
    assert st["profile"] == "production" and st["ok"] is False
    assert any("not durable" in w for w in st["warnings"]) and any("per-process" in w for w in st["warnings"])


def test_startup_announcement_is_loud_in_development_and_once_per_component(monkeypatch, caplog):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    monkeypatch.setattr(pr, "_announced", set())
    with caplog.at_level(logging.INFO, logger="agentic_os.profile"):
        pr.announce("svc-a")
        pr.announce("svc-a")
        pr.announce("svc-b")
    warned = [r for r in caplog.records if r.levelno == logging.WARNING and "security profile" in r.getMessage()]
    assert len(warned) == 2 and "svc-a" in warned[0].getMessage() and "not enforced" in warned[0].getMessage()


def test_control_plane_health_shows_the_security_posture_only_to_a_credentialed_caller(monkeypatch, tmp_path):
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))      # the module creates these at import
    monkeypatch.setenv("AGENTIC_OS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTIC_OS_API_KEY", "fleet-key-DO-NOT-STORE")
    from fastapi.testclient import TestClient
    from agentic_os import control_plane
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    client = TestClient(control_plane.app)
    anonymous = client.get("/health").json()
    assert set(anonymous) == {"status", "modules"} and anonymous["status"] == "ok"            # liveness only: no posture for an anonymous caller
    body = client.get("/health", headers={"X-API-Key": "fleet-key-DO-NOT-STORE"}).json()
    assert body["status"] == "ok" and body["security"]["profile"] == "development" and body["security"]["ok"] is False


GOOD = {"AGENTIC_OS_PROFILE": "production", "AGENTIC_OS_AUTHORITY_DB": "/tmp/authority.db",
        "AGENTIC_OS_REPLAY_DB": "/tmp/replay.db", "AGENTIC_OS_APPROVAL_SECRET": "x" * 40,
        "AGENTIC_OS_CONTROL_TOKEN": "tok"}


def test_deployment_check_passes_a_complete_production_environment():
    assert pr.check_environment(GOOD) == []


def test_deployment_check_names_every_missing_production_requirement():
    probs = pr.check_environment({"AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS": "1"})
    text = " | ".join(probs)
    for needle in ("'production'", "AGENTIC_OS_AUTHORITY_DB", "AGENTIC_OS_REPLAY_DB", "AGENTIC_OS_APPROVAL_SECRET",
                   "CONTROL_TOKEN", "ALLOW_ANONYMOUS"):
        assert needle in text, needle


def test_deployment_check_rejects_unwritable_store_paths_and_short_secrets():
    probs = pr.check_environment({**GOOD, "AGENTIC_OS_AUTHORITY_DB": "/definitely/not/a/dir/a.db",
                                  "AGENTIC_OS_APPROVAL_SECRET": "short"})
    assert any("not writable" in p or "does not exist" in p for p in probs) and any("shorter than 32" in p for p in probs)


def test_cli_exit_codes(monkeypatch, capsys):
    monkeypatch.delenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", raising=False)     # some suites set it globally
    monkeypatch.delenv("AGENTIC_OS_TENANT_TOKENS", raising=False)
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    assert pr.main(["--require-production"]) == 0
    monkeypatch.delenv("AGENTIC_OS_AUTHORITY_DB")
    assert pr.main(["--require-production"]) == 1
    assert "AGENTIC_OS_AUTHORITY_DB" in capsys.readouterr().out
    assert pr.main([]) == 0                               # plain run only reports


def test_egress_proxy_is_reported_without_leaking_credentials():
    assert pr.egress_proxy({}) == ""
    assert pr.egress_proxy({"HTTPS_PROXY": "http://egress:3128"}) == "egress:3128"
    assert pr.egress_proxy({"https_proxy": "http://user:s3cretpass@proxy.internal:8080"}) == "proxy.internal:8080"
    assert pr.egress_proxy({"HTTPS_PROXY": "egress"}) == "egress"


def test_posture_advises_in_production_when_no_proxy_and_the_preflight_can_require_one(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    st = pr.status()
    assert st["egress_proxy_configured"] is False and any("egress proxy" in a for a in st["advisories"])
    assert not any("egress" in w for w in st["warnings"])                                  # advisory: does not flip ok
    assert not any("egress" in p for p in pr.check_environment(GOOD))                        # advisory by default
    assert any("egress proxy" in p for p in pr.check_environment(GOOD, require_egress=True))
    assert pr.check_environment({**GOOD, "HTTPS_PROXY": "http://egress:3128"}, require_egress=True) == []
    monkeypatch.setenv("HTTPS_PROXY", "http://egress:3128")
    assert pr.status()["egress_proxy_configured"] is True


def test_cli_require_egress_flag(monkeypatch, capsys):
    monkeypatch.delenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.delenv("AGENTIC_OS_TENANT_TOKENS", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    assert pr.main(["--require-production"]) == 0
    assert pr.main(["--require-production", "--require-egress"]) == 1
    assert "egress proxy" in capsys.readouterr().out
    monkeypatch.setenv("HTTPS_PROXY", "http://egress:3128")
    assert pr.main(["--require-production", "--require-egress"]) == 0
