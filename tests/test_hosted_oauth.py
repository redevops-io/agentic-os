"""Hosted OAuth bootstrap — the served-app Connect orchestration, unit-tested with a fake
session factory (no browser, no connector, no live provider). Proves: start stores by CSRF
state and returns the consent URL; the callback dispatches, marks connected, and bridges the
fetched token to go_live via <provider>:token; unavailable providers and unknown states are
refused; app config is read from env."""
from __future__ import annotations

import pytest

from agentic_os.integrations.connect import ConnectOutcome
from agentic_os.integrations.hosted_oauth import HostedConnect, ProviderOAuthApp, oauth_apps_from_env


class FakeSession:
    def __init__(self, provider, state="S1", ref="ref-1", connected=True):
        self.provider, self.state, self.ref, self._ok = provider, state, ref, connected

    def start(self):
        return {"authorize_url": f"https://provider/authorize?client_id=x&state={self.state}", "state": self.state}

    def complete(self, code, state):
        if not self._ok:
            return ConnectOutcome(self.provider, "NOT_CONNECTED", False, detail="declined")
        return ConnectOutcome(self.provider, "VERIFIED_READ", True, credential_ref=self.ref)


class FakeBroker:
    class _R:
        def resolve(self, ref):
            return {"access_token": f"tok-for-{ref}"}
    resolver = _R()


def _hc(connected=True):
    return HostedConnect(session_factory=lambda p: FakeSession(p, connected=connected),
                         providers=("slack",), broker=FakeBroker())


def test_start_returns_consent_url_and_stores_state():
    hc = _hc()
    started = hc.start("slack")
    assert "state=S1" in started["authorize_url"] and started["state"] == "S1"


def test_callback_connects_and_bridges_the_token_to_go_live():
    hc = _hc()
    hc.start("slack")
    out = hc.callback("auth_code", "S1")
    assert out.connected and out.provider == "slack"
    assert hc.connected_providers() == ("slack",)
    # go_live resolves <provider>:token — the hosted flow makes it resolve the fetched token
    assert hc.resolver().resolve("slack:token") == {"access_token": "tok-for-ref-1"}


def test_unavailable_provider_is_refused():
    with pytest.raises(KeyError):
        _hc().start("hubspot")   # no OAuth app registered


def test_unknown_state_is_not_connected():
    out = _hc().callback("code", "never-issued")
    assert not out.connected and "unknown or expired state" in out.detail


def test_declined_connect_is_not_recorded():
    hc = _hc(connected=False)
    hc.start("slack")
    out = hc.callback("code", "S1")
    assert not out.connected and hc.connected_providers() == ()
    with pytest.raises(KeyError):
        hc.resolver().resolve("slack:token")   # nothing to resolve


def test_oauth_apps_from_env_builds_the_slack_app_with_the_hosted_callback():
    apps = oauth_apps_from_env("https://projects.example.com/",
                               env={"SLACK_CLIENT_ID": "cid", "SLACK_CLIENT_SECRET": "sec"})
    app = apps["slack"]
    assert isinstance(app, ProviderOAuthApp)
    assert app.redirect_uri == "https://projects.example.com/api/apps/connect/callback"
    assert app.scopes == ("chat:write,channels:history,users:read",)   # comma form for Slack v2
    # a provider with no client creds in env isn't offered
    assert oauth_apps_from_env("https://x/", env={}) == {}
