"""The opt-in enforcement page: off by default, documentation addresses only, read-only, and honest when the edge cannot be read."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

app_mod = importlib.import_module("edge-sentinel.app")
core = importlib.import_module("edge-sentinel.core")
probe = importlib.import_module("edge-sentinel.enforcement_probe")
fixture_mode = importlib.import_module("edge-sentinel.fixture_mode")

DOC = "203.0.113.7"


class _Lapi:
    """Stands in for the CrowdSec decisions API: records every request, answers from `bans`."""
    bans: set = set()
    calls: list = []
    fail = False

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, params=None):
        type(self).calls.append(("GET", url, dict(params or {})))
        if type(self).fail:
            raise OSError("connection refused")
        ip = (params or {}).get("ip")

        class R:
            status_code = 200

            @staticmethod
            def json():
                return [{"type": "ban", "scope": "Ip", "value": ip}] if ip in _Lapi.bans else None
        return R()

    def __getattr__(self, name):                      # a write of any kind would be a defect of a read-only page
        raise AssertionError(f"the probe called client.{name}")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SENTINEL_RATE_BURST", "1000000")
    monkeypatch.setenv("SENTINEL_RATE_PER_MIN", "1000000000")
    monkeypatch.delenv("SENTINEL_ENFORCEMENT_PROBE", raising=False)
    monkeypatch.delenv("SENTINEL_REQUIRE_AUTH", raising=False)
    _Lapi.bans, _Lapi.calls, _Lapi.fail = set(), [], False
    monkeypatch.setattr(probe.httpx, "Client", _Lapi)
    return TestClient(app_mod.app)


def test_off_by_default_the_page_does_not_exist_and_health_says_so(client):
    assert client.get(f"/enforcement/{DOC}").status_code == 404
    assert client.get("/health").json()["enforcement_probe"]["enabled"] is False
    assert _Lapi.calls == [], "a disabled page must not read the edge"


def test_on_it_reports_the_read_back_of_the_real_decision_list(client, monkeypatch):
    monkeypatch.setenv("SENTINEL_ENFORCEMENT_PROBE", "1")
    assert client.get("/health").json()["enforcement_probe"]["enabled"] is True
    r = client.get(f"/enforcement/{DOC}")
    assert r.status_code == 200 and 'data-testid="enforcement-state">not blocked<' in r.text and r.headers["cache-control"] == "no-store"
    _Lapi.bans.add(DOC)
    r = client.get(f"/enforcement/{DOC}")
    assert r.status_code == 200 and 'data-testid="enforcement-state">blocked<' in r.text
    assert all(c[0] == "GET" and c[1].endswith("/v1/decisions") and c[2] == {"ip": DOC} for c in _Lapi.calls) and len(_Lapi.calls) == 2


@pytest.mark.parametrize("address", ["8.8.8.8", "10.0.0.1", "127.0.0.1", "203.0.114.1", "203.0.113.0%2F24", "2001:4860::1", "not-an-address", "203.0.113.7x"])
def test_only_a_documentation_address_can_be_asked_about(client, monkeypatch, address):
    monkeypatch.setenv("SENTINEL_ENFORCEMENT_PROBE", "1")
    assert client.get(f"/enforcement/{address}").status_code == 404
    assert _Lapi.calls == [], "nothing is read for an address outside the documentation ranges"


@pytest.mark.parametrize("address", ["192.0.2.1", "198.51.100.9", "203.0.113.255", "2001:db8::1"])
def test_every_documentation_range_is_accepted(client, monkeypatch, address):
    monkeypatch.setenv("SENTINEL_ENFORCEMENT_PROBE", "1")
    assert client.get(f"/enforcement/{address}").status_code == 200


def test_an_edge_that_cannot_be_read_is_unknown_never_not_blocked(client, monkeypatch):
    monkeypatch.setenv("SENTINEL_ENFORCEMENT_PROBE", "1")
    _Lapi.fail = True
    r = client.get(f"/enforcement/{DOC}")
    assert r.status_code == 503 and 'data-testid="enforcement-state">unknown<' in r.text and "not blocked" not in r.text


def test_it_is_never_on_with_the_fixture_core(client, monkeypatch):
    monkeypatch.setenv("SENTINEL_ENFORCEMENT_PROBE", "1")
    monkeypatch.setattr(fixture_mode, "_INSTALLED", {"core": True})
    assert probe.enabled() is False and client.get(f"/enforcement/{DOC}").status_code == 404
    assert client.get("/health").json()["enforcement_probe"]["enabled"] is False


def test_with_sign_in_required_the_page_is_public_only_while_the_probe_is_on(client, monkeypatch):
    monkeypatch.setenv("SENTINEL_REQUIRE_AUTH", "1")
    assert client.get(f"/enforcement/{DOC}").status_code == 401            # off: an unknown path like any other, behind the sign-in
    monkeypatch.setenv("SENTINEL_ENFORCEMENT_PROBE", "1")
    assert client.get(f"/enforcement/{DOC}").status_code == 200
    assert client.get("/api/activity").status_code == 401, "turning the probe on opens nothing else"


def test_the_page_only_accepts_get(client, monkeypatch):
    monkeypatch.setenv("SENTINEL_ENFORCEMENT_PROBE", "1")
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)(f"/enforcement/{DOC}").status_code == 405
