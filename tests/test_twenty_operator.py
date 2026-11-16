"""Twenty CRM — the approval-gated execution adapter operator. Proves the gate (unapproved invokes never
write) and shadow mode (even an approved invoke doesn't touch a real Twenty while shadow is on)."""
from __future__ import annotations

from agentic_os.integrations.twenty_operator import build_twenty_operator, core_upsert_contact
from agentic_os.integrations.twenty_write import TwentyWriteClient


def test_manifest_declares_approval_required_and_side_effecting():
    m = build_twenty_operator().manifest
    assert m.operator == "twenty-crm"
    spec = m.capabilities[0]
    assert spec.name == "crm.contact.upsert"
    assert spec.approval_required is True
    assert spec.side_effecting is True
    assert "crm:write" in spec.permissions


def test_invoke_without_approval_stages_and_does_not_write():
    client = TwentyWriteClient(base_url="http://twenty.example", api_key="k", shadow=False)
    op = build_twenty_operator(client=client)
    res = op.invoke("crm.contact.upsert", {"name": "Dana Rivera", "email": "dana@example.com"})
    assert res["status"] == "pending_approval" and res["executed"] is False
    assert res["contact_upserted"] is False


def test_invoke_with_approval_but_shadow_mode_does_not_call_twenty():
    client = TwentyWriteClient(base_url="http://twenty.example", api_key="k", shadow=True)
    op = build_twenty_operator(client=client)
    res = op.invoke("crm.contact.upsert", {"name": "Dana Rivera", "email": "dana@example.com",
                                           "_approval": {"approved": True}})
    assert res["status"] == "done" and res["shadow"] is True
    assert res["would_upsert"] == "person"


def test_core_upsert_contact_validates_before_touching_client():
    res = core_upsert_contact({"name": "", "email": "dana@example.com"})
    assert res["status"] == "error" and "malformed" in res["error"]
    res = core_upsert_contact({"name": "Dana Rivera", "email": "not-an-email"})
    assert res["status"] == "error"


def test_core_upsert_contact_without_configured_client_errors_cleanly(monkeypatch):
    monkeypatch.delenv("TWENTY_BASE_URL", raising=False)
    monkeypatch.delenv("TWENTY_API_KEY", raising=False)
    res = core_upsert_contact({"name": "Dana Rivera", "email": "dana@example.com"})
    assert res["status"] == "error" and res["error"] == "twenty_unconfigured"


def test_invoke_with_approval_live_mode_creates_person(monkeypatch):
    import types

    posted = []

    def fake_get(url, params=None, headers=None, timeout=None):
        class _R:
            status_code = 200
            def json(self):
                return {"data": {"people": []}}
        return _R()

    def fake_post(url, json=None, headers=None, timeout=None):
        posted.append(json)
        class _R:
            status_code = 200
            def json(self):
                return {"data": {"createPerson": {"id": "p1"}}}
        return _R()

    import agentic_os.integrations.twenty_write as tw_mod
    monkeypatch.setattr(tw_mod, "httpx", types.SimpleNamespace(get=fake_get, post=fake_post))

    client = TwentyWriteClient(base_url="http://twenty.example", api_key="k", shadow=False)
    op = build_twenty_operator(client=client)
    res = op.invoke("crm.contact.upsert", {"name": "Dana Rivera", "email": "dana@example.com",
                                           "_approval": {"approved": True}})
    assert res["status"] == "done" and res["created"] is True
    assert posted and posted[0]["emails"]["primaryEmail"] == "dana@example.com"
