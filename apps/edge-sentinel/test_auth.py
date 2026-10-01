"""The operator auth gate: fail-closed 503 when unconfigured, 401 without/with bad Basic creds, 200 (returns
username) on a match, PARTNERS_* fallback, and the independent dry-run switch."""
from __future__ import annotations

import base64
import importlib

import pytest
from fastapi import HTTPException

auth = importlib.import_module("edge-sentinel.auth")

_CREDS = ("SENTINEL_BASIC_AUTH_USER", "SENTINEL_BASIC_AUTH_PASS",
          "PARTNERS_BASIC_AUTH_USER", "PARTNERS_BASIC_AUTH_PASS")


class _Req:
    def __init__(self, authz: str | None = None):
        self.headers = {"authorization": authz} if authz else {}


def _basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def test_503_when_unconfigured(monkeypatch):
    for k in _CREDS:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(HTTPException) as ei:
        auth.require_operator(_Req())
    assert ei.value.status_code == 503


def test_401_without_header(monkeypatch):
    for k in _CREDS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_USER", "op")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_PASS", "pw")
    with pytest.raises(HTTPException) as ei:
        auth.require_operator(_Req())
    assert ei.value.status_code == 401
    assert "WWW-Authenticate" in (ei.value.headers or {})


def test_401_bad_creds(monkeypatch):
    for k in _CREDS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_USER", "op")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_PASS", "pw")
    with pytest.raises(HTTPException) as ei:
        auth.require_operator(_Req(_basic("op", "wrong")))
    assert ei.value.status_code == 401


def test_200_good_creds(monkeypatch):
    for k in _CREDS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_USER", "op")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_PASS", "pw")
    assert auth.require_operator(_Req(_basic("op", "pw"))) == "op"


def test_partners_credential_fallback(monkeypatch):
    for k in _CREDS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PARTNERS_BASIC_AUTH_USER", "po")
    monkeypatch.setenv("PARTNERS_BASIC_AUTH_PASS", "pp")
    assert auth.require_operator(_Req(_basic("po", "pp"))) == "po"


def test_block_enabled_default_false(monkeypatch):
    monkeypatch.delenv("SENTINEL_BLOCK_ENABLED", raising=False)
    assert auth.block_enabled() is False
    monkeypatch.setenv("SENTINEL_BLOCK_ENABLED", "true")
    assert auth.block_enabled() is True
