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
