"""Test-suite fixtures.

Keep the suite deterministic + offline: the World Adapter registry prefers a real OSS core whenever its
env creds are present + reachable, and those creds may be exported in the developer's environment (to wire a
live demo). Clear them before every test so projections always use the in-memory demo store — tests that
exercise a real adapter opt back in with an explicit ``monkeypatch.setenv``.
"""
import pytest

_CORE_ENV = ("TWENTY_BASE_URL", "TWENTY_API_KEY", "LAGO_API_URL", "LAGO_API_KEY",
             "CHATWOOT_BASE_URL", "CHATWOOT_API_TOKEN", "CHATWOOT_ACCOUNT_ID")


@pytest.fixture(autouse=True)
def _no_live_cores(monkeypatch):
    for e in _CORE_ENV:
        monkeypatch.delenv(e, raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_platform_secrets(monkeypatch, tmp_path):
    """Never read/write the developer's real secrets file, rc files or keyring (docs/SECRETS.md)."""
    monkeypatch.setenv("REDEVOPS_SECRETS_AUTO", "0")
    monkeypatch.setenv("REDEVOPS_SECRETS_FILE", str(tmp_path / "no-such-dir" / "secrets.env"))
    monkeypatch.setenv("REDEVOPS_SECRETS_NO_KEYRING", "1")
    for e in ("VAULT_ADDR", "VAULT_TOKEN", "AGENTIC_OS_APPROVAL_SECRET"):
        monkeypatch.delenv(e, raising=False)
    yield
