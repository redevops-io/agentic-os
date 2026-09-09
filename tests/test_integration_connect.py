"""The one-click Connect acceptance path (agentic_os.integrations.connect). The
orchestration is exercised with fakes — no browser, no live provider, no connector import
— proving the frozen path: authorize → CredentialRef → build adapter on that ref →
verify_setup → graded SetupState."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from agentic_os.integrations.connect import ConnectOutcome, connect_provider


@dataclass
class _Result:
    credential_ref: str
    account_ref: str = ""
    scopes: Tuple[str, ...] = ()


class _Runner:
    def __init__(self, result: _Result):
        self._r = result

    def run(self) -> _Result:
        return self._r


@dataclass
class _Setup:
    state: str
    connected: bool
    detail: str = ""


def test_acceptance_path_connects_and_verifies():
    seen = {}

    def build_adapter(ref: str):
        seen["ref"] = ref  # adapter is built on the FETCHED credential ref, nothing pasted
        return object()

    out = connect_provider(
        "slack",
        runner=_Runner(_Result("slack:oauth:1", account_ref="T9", scopes=("chat:write", "im:write"))),
        build_adapter=build_adapter,
        verify=lambda _a: _Setup("VERIFIED_READ", True, "auth.test ok"),
    )
    assert isinstance(out, ConnectOutcome)
    assert out.connected and out.state == "VERIFIED_READ"
    assert out.credential_ref == "slack:oauth:1" and seen["ref"] == "slack:oauth:1"
    assert out.account_ref == "T9" and out.scopes == ("chat:write", "im:write")


def test_no_credential_returns_not_connected_without_building_an_adapter():
    built = {"n": 0}

    def build_adapter(_ref: str):
        built["n"] += 1
        return object()

    out = connect_provider("slack", runner=_Runner(_Result("")),
                           build_adapter=build_adapter, verify=lambda _a: _Setup("x", True))
    assert not out.connected and out.state == "NOT_CONNECTED"
    assert built["n"] == 0  # never verify a connection that produced no credential


def test_failed_verify_is_reported_as_not_verified():
    out = connect_provider("slack", runner=_Runner(_Result("slack:oauth:2")),
                           build_adapter=lambda _r: object(),
                           verify=lambda _a: _Setup("NOT_CONNECTED", False, "invalid_auth"))
    assert out.credential_ref == "slack:oauth:2"  # a token was fetched…
    assert not out.connected and out.state == "NOT_CONNECTED" and out.detail == "invalid_auth"  # …but it didn't verify


def test_enum_like_state_is_unwrapped_to_its_value():
    class _EnumLike:
        value = "VERIFIED_READ"

    out = connect_provider("slack", runner=_Runner(_Result("slack:oauth:3")),
                           build_adapter=lambda _r: object(),
                           verify=lambda _a: _Setup(_EnumLike(), True))
    assert out.state == "VERIFIED_READ"


# ── hosted-callback session (served app: two HTTP steps, no loopback) ──
class _FakeGrant:
    def __init__(self, token, account_ref="T1", scopes=("chat:write",)):
        self.access_token = token
        self.account_ref = account_ref
        self.scopes = scopes


class _FakeFlow:
    def authorize_url(self, *, state):
        return f"https://provider/authorize?client_id=x&state={state}"

    def exchange_code(self, code):
        return _FakeGrant(f"tok-for-{code}")


class _FakeBroker:
    def __init__(self):
        self.resolver = object()
        self._n = 0

    def store(self, provider, grant):
        self._n += 1
        return f"{provider}:oauth:{self._n}"


def _session(state="S1"):
    from agentic_os.integrations.connect import HostedConnectSession
    return HostedConnectSession(
        provider="slack", flow=_FakeFlow(), broker=_FakeBroker(),
        build_adapter=lambda ref, resolver: ("adapter", ref),
        verify=lambda _a: _Setup("VERIFIED_READ", True, "auth.test ok"),
        state_factory=lambda: state,
    )


def test_hosted_start_returns_consent_url_with_state():
    s = _session().start()
    assert s["state"] == "S1" and "state=S1" in s["authorize_url"]


def test_hosted_complete_verifies_and_returns_outcome():
    sess = _session()
    sess.start()
    out = sess.complete("code123", "S1")
    assert out.connected and out.state == "VERIFIED_READ"
    assert out.credential_ref == "slack:oauth:1" and out.account_ref == "T1"


def test_hosted_complete_refuses_state_mismatch():
    sess = _session()
    sess.start()
    out = sess.complete("code123", "WRONG")
    assert not out.connected and "state mismatch" in out.detail


def test_hosted_complete_before_start_is_refused():
    out = _session().complete("code123", "S1")  # no start() → no stored state
    assert not out.connected and "state mismatch" in out.detail


def test_hosted_complete_without_code_is_refused():
    sess = _session()
    sess.start()
    out = sess.complete("", "S1")
    assert not out.connected and "no authorization code" in out.detail
