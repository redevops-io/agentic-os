"""N7: every model call of agentic-crm routes through agentic_os.app.llm.governed_text as CUSTOMER_CONFIDENTIAL and fails
closed (no in-boundary endpoint / router refusal => deterministic text), never to a direct HTTP client."""
from __future__ import annotations

import importlib

import pytest

app_mod = importlib.import_module("agentic-crm.app")


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


def test_research_and_draft_report_llm_unavailable_and_write_nothing(monkeypatch):
    import types
    core = importlib.import_module("agentic-crm.core")

    class _R:
        def __init__(self, s=200, p=None): self.status_code, self._p = s, p or {}
        def json(self): return self._p

    class _C:
        writes = []
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None, params=None): return _R(200, {"data": {"name": "L1", "lead_name": "A"}})
        def post(self, url, headers=None, json=None): _C.writes.append(url); return _R(200, {})
        def put(self, url, headers=None, json=None): _C.writes.append(url); return _R(200, {})

    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(Client=lambda timeout=None: _C()))
    for fn in (app_mod._research, app_mod._draft_outreach):
        assert fn({"lead": "L1"})["status"] == "llm_unavailable"
    assert _C.writes == []                                                # nothing written without a model
    assert app_mod._score_lead({"lead": "L1"})["method"] == "rules"      # deterministic fallback

