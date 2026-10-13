"""N7: every model call of support routes through agentic_os.app.llm.governed_text as CUSTOMER_CONFIDENTIAL and fails
closed (no in-boundary endpoint / router refusal => deterministic text), never to a direct HTTP client."""
from __future__ import annotations

import importlib

import pytest

app_mod = importlib.import_module("support.app")


@pytest.fixture(autouse=True)
def _no_endpoint(monkeypatch):
    for k in ("REDEVOPS_LLM_BASE_URL", "REDEVOPS_LLM_MODEL", "REDEVOPS_LLM_API_KEY", "OPENAI_API_KEY",
              "ANTHROPIC_API_KEY", "REDEVOPS_PRIVACY_MODE"):
        monkeypatch.delenv(k, raising=False)


def test_routes_through_governed_text_as_customer_confidential(monkeypatch):
    import agentic_os.app.llm as llm
    from agentic_os.governance.classification import DataClassification
    seen = {}

    def fake(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return "governed reply"

    monkeypatch.setattr(llm, "governed_text", fake)
    assert app_mod._llm_text("customer says hi") in ("governed reply",)
    assert seen["classifications"] == (DataClassification.CUSTOMER_CONFIDENTIAL,)


def test_fails_closed_without_an_in_boundary_endpoint():
    assert not app_mod._llm_text("customer says hi")


def test_public_endpoint_is_not_in_boundary(monkeypatch):
    monkeypatch.setenv("REDEVOPS_LLM_BASE_URL", "https://api.openai.com/v1")
    assert not app_mod._llm_text("customer says hi")


def test_draft_reply_degrades_to_the_deterministic_template(monkeypatch):
    import types
    from support import core

    class _R:
        def __init__(self, s=200, p=None): self.status_code, self._p = s, p or {}
        def json(self): return self._p

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None, params=None):
            return _R(200, {"meta": {"sender": {"name": "Dana Smith"}}, "messages": [{"message_type": 0, "content": "hi"}]})
        def post(self, url, headers=None, json=None): return _R(200, {})

    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(Client=lambda timeout=None: _C()))
    res = app_mod._draft_reply({"conversation_id": 1})
    assert res["draft_source"] == "deterministic template" and "claude" not in res["draft_source"].lower()

