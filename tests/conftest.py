"""Test-suite fixtures.

Keep the suite deterministic + offline: the World Adapter registry prefers a real OSS core whenever its
env creds are present + reachable, and those creds may be exported in the developer's environment (to wire a
live demo). Clear them before every test so projections always use the in-memory demo store — tests that
exercise a real adapter opt back in with an explicit ``monkeypatch.setenv``.
"""
import os
import re

import pytest

# --- live tests are opt-in (R0-prime): an ambient credential alone never triggers a live call ------------------------
# Tests marked ``live`` (real provider/bot/infrastructure calls) run only with REDEVOPS_ALLOW_LIVE_TESTS=1. Unless that
# variable is set, every credential-looking environment variable (API keys, tokens, secrets, passwords, SSH agent socket)
# is also removed from the process for the whole session, so no test or subprocess it starts can pick one up by accident.
# Tests that need a value set it themselves (monkeypatch.setenv after this runs).
ALLOW_LIVE = os.environ.get("REDEVOPS_ALLOW_LIVE_TESTS") == "1"
_CRED_NAME = re.compile(r"(API_KEY|API_TOKEN|_TOKEN|TOKEN|_SECRET|SECRET_KEY|SECRET_ACCESS_KEY|ACCESS_KEY|PASSWORD|CREDENTIALS?|PRIVATE_KEY|API_KEY_ID)$"
                        r"|^(SSH_AUTH_SOCK|SSH_AGENT_PID|APPROVAL_SECRET)$", re.I)
_HIDDEN: dict = {}


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls a real external service or infrastructure; runs only with REDEVOPS_ALLOW_LIVE_TESTS=1")
    if not ALLOW_LIVE:
        for name in [n for n in os.environ if _CRED_NAME.search(n)]:
            _HIDDEN[name] = os.environ.pop(name)


def pytest_unconfigure(config):
    os.environ.update(_HIDDEN)
    _HIDDEN.clear()


def pytest_report_header(config):
    return ("live tests: " + ("ENABLED (REDEVOPS_ALLOW_LIVE_TESTS=1)" if ALLOW_LIVE else
            f"disabled (set REDEVOPS_ALLOW_LIVE_TESTS=1); {len(_HIDDEN)} credential-like environment variable(s) hidden from the session (names not shown)"))


def pytest_collection_modifyitems(config, items):
    if ALLOW_LIVE:
        return
    skip = pytest.mark.skip(reason="live test: set REDEVOPS_ALLOW_LIVE_TESTS=1 (and its own credentials) to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


_CORE_ENV = ("TWENTY_BASE_URL", "TWENTY_API_KEY", "LAGO_API_URL", "LAGO_API_KEY",
             "CHATWOOT_BASE_URL", "CHATWOOT_API_TOKEN", "CHATWOOT_ACCOUNT_ID")


@pytest.fixture(autouse=True)
def _no_live_cores(monkeypatch):
    for e in _CORE_ENV:
        monkeypatch.delenv(e, raising=False)
    yield
