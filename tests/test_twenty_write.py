"""Twenty CRM write client — shadow mode by default, real upsert mechanics behind a fake transport."""
from __future__ import annotations

import pytest

from agentic_os.integrations import twenty_write as tw


def test_shadow_mode_enabled_by_default(monkeypatch):
    monkeypatch.delenv("TWENTY_WRITE_SHADOW_MODE", raising=False)
    assert tw.shadow_mode_enabled() is True


@pytest.mark.parametrize("value,expected", [("false", False), ("False", False), ("0", False), ("no", False),
                                           ("true", True), ("", True), ("anything-else", True)])
def test_shadow_mode_env_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("TWENTY_WRITE_SHADOW_MODE", value)
    assert tw.shadow_mode_enabled() is expected


def test_shadow_upsert_person_never_calls_http():
    client = tw.TwentyWriteClient(base_url="http://twenty.example", api_key="k", shadow=True)
    res = client.upsert_person(name="Dana Rivera", email="dana@example.com")
    assert res == {"shadow": True, "would_upsert": "person", "name": "Dana Rivera",
                   "email": "dana@example.com", "company_id": None}


def test_shadow_upsert_company_never_calls_http():
    client = tw.TwentyWriteClient(base_url="http://twenty.example", api_key="k", shadow=True)
    res = client.upsert_company(name="Acme", domain="acme.com")
    assert res == {"shadow": True, "would_upsert": "company", "name": "Acme", "domain": "acme.com"}


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload if payload is not None else {}

    def json(self):
        return self._p


def test_live_upsert_person_creates_when_not_found(monkeypatch):
    calls = {"get": [], "post": []}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["get"].append((url, params))
        return _Resp(200, {"data": {"people": []}})

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["post"].append((url, json))
        return _Resp(200, {"data": {"createPerson": {"id": "p1"}}})

    monkeypatch.setattr(tw, "httpx", __import__("types").SimpleNamespace(get=fake_get, post=fake_post))
    client = tw.TwentyWriteClient(base_url="http://twenty.example", api_key="k", shadow=False)
    res = client.upsert_person(name="Dana Rivera", email="dana@example.com")
    assert res["shadow"] is False and res["created"] is True
    assert res["person"] == {"id": "p1"}
    assert calls["post"][0][1]["emails"]["primaryEmail"] == "dana@example.com"
    assert calls["post"][0][1]["name"] == {"firstName": "Dana", "lastName": "Rivera"}


def test_live_upsert_person_matches_existing_without_posting(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return _Resp(200, {"data": {"people": [{"id": "p-existing"}]}})

    def fake_post(url, json=None, headers=None, timeout=None):
        raise AssertionError("must not POST when a matching person already exists")

    monkeypatch.setattr(tw, "httpx", __import__("types").SimpleNamespace(get=fake_get, post=fake_post))
    client = tw.TwentyWriteClient(base_url="http://twenty.example", api_key="k", shadow=False)
    res = client.upsert_person(name="Dana Rivera", email="dana@example.com")
    assert res == {"shadow": False, "created": False, "person": {"id": "p-existing"}}


def test_live_upsert_person_http_error_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(tw, "httpx", __import__("types").SimpleNamespace(
        get=lambda *a, **k: _Resp(200, {"data": {"people": []}}),
        post=lambda *a, **k: _Resp(500, {})))
    client = tw.TwentyWriteClient(base_url="http://twenty.example", api_key="k", shadow=False)
    res = client.upsert_person(name="Dana Rivera", email="dana@example.com")
    assert res["created"] is False and "error" in res


def test_sequential_upsert_person_same_email_creates_at_most_once(monkeypatch):
    """Governance review (Stage E architecture decision, 2026-11-16): the direct approval-gate path
    relies on this find-or-create property, not a Dagster/Mission idempotency key, to guarantee a
    second approved upsert for the same email never creates a duplicate Person. Exercise it as two
    real sequential calls, the second seeing the first's created record, rather than inferring it from
    two separately-framed tests."""
    store: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        existing = [store["person"]] if "person" in store else []
        return _Resp(200, {"data": {"people": existing}})

    def fake_post(url, json=None, headers=None, timeout=None):
        person = {"id": "p1", **json}
        store["person"] = person
        return _Resp(200, {"data": {"createPerson": person}})

    monkeypatch.setattr(tw, "httpx", __import__("types").SimpleNamespace(get=fake_get, post=fake_post))
    client = tw.TwentyWriteClient(base_url="http://twenty.example", api_key="k", shadow=False)

    first = client.upsert_person(name="Dana Rivera", email="dana@example.com")
    second = client.upsert_person(name="Dana Rivera", email="dana@example.com")

    assert first["created"] is True
    assert second["created"] is False and second["person"]["id"] == "p1"


def test_twenty_write_from_env_requires_both_vars(monkeypatch):
    monkeypatch.delenv("TWENTY_BASE_URL", raising=False)
    monkeypatch.delenv("TWENTY_API_KEY", raising=False)
    assert tw.twenty_write_from_env() is None
    monkeypatch.setenv("TWENTY_BASE_URL", "http://twenty.example")
    monkeypatch.setenv("TWENTY_API_KEY", "k")
    monkeypatch.setenv("TWENTY_WRITE_SHADOW_MODE", "false")
    client = tw.twenty_write_from_env()
    assert client is not None and client.shadow is False
