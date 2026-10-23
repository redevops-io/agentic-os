"""chase_overdue reports what Lago answered: a rejected retry is never "done" (audit: false success)."""
from __future__ import annotations

import types

import pytest

from billing import core
from billing.test_operator import INVOICES, _FakeClient, _Resp, _fake_get


class _RejectingClient(_FakeClient):
    status = 422

    def post(self, url, headers=None, json=None):
        _FakeClient.posted.append(url)
        return _Resp(self.status)


class _RaisingClient(_FakeClient):
    def post(self, url, headers=None, json=None):
        raise TimeoutError("lago down")


def _use(monkeypatch, cls):
    _FakeClient.posted = []
    core._CACHE.update(ts=0.0, data=None)
    monkeypatch.setattr(core, "LAGO_API_KEY", "test-key")
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(get=_fake_get, Client=lambda timeout=None: cls()))


def test_accepted_retry_is_done(monkeypatch):
    _use(monkeypatch, _FakeClient)
    out = core.chase_overdue(approval={"approved": True})
    assert out["status"] == "done" and out["accepted_count"] == len(INVOICES)
    assert "retry requested" in out["results"][0]["action"] and "reminder sent" not in out["summary"]


@pytest.mark.parametrize("cls", [_RejectingClient, _RaisingClient])
def test_rejected_or_failed_retry_is_not_reported_as_done(monkeypatch, cls):
    _use(monkeypatch, cls)
    out = core.chase_overdue(approval={"approved": True})
    assert out["status"] == "failed" and out["accepted_count"] == 0
    assert out["results"][0]["accepted"] is False
    assert "not accepted" in out["summary"]


def test_partial_acceptance_is_partial(monkeypatch):
    class _Half(_FakeClient):
        n = 0

        def post(self, url, headers=None, json=None):
            _Half.n += 1
            return _Resp(200 if _Half.n % 2 else 500)

    two = INVOICES + [{**INVOICES[0], "number": "INV-002", "lago_id": "lago-2"}]
    _use(monkeypatch, _Half)
    monkeypatch.setattr(_Half, "get", lambda self, url, headers=None, params=None:
                        _Resp(200, {"invoices": two, "meta": {"total_pages": 1}}) if url.endswith("/api/v1/invoices")
                        else _Resp(200, {"customers": [{"name": "Whitfield Family Trust"}], "meta": {"total_pages": 1}}))
    out = core.chase_overdue(approval={"approved": True})
    assert out["status"] == "partial" and out["accepted_count"] == 1
