"""N7: every model call of outreach-engine routes through agentic_os.app.llm.governed_text as CUSTOMER_CONFIDENTIAL and fails
closed (no in-boundary endpoint / router refusal => deterministic text), never to a direct HTTP client."""
from __future__ import annotations

import importlib

import pytest

app_mod = importlib.import_module("outreach-engine.app")


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
    assert app_mod._llm("customer says hi") in ("governed reply",)
    assert seen["classifications"] == (DataClassification.CUSTOMER_CONFIDENTIAL,)


def test_fails_closed_without_an_in_boundary_endpoint():
    assert not app_mod._llm("customer says hi")


def test_public_endpoint_is_not_in_boundary(monkeypatch):
    monkeypatch.setenv("REDEVOPS_LLM_BASE_URL", "https://api.openai.com/v1")
    assert not app_mod._llm("customer says hi")


def test_no_hardcoded_default_model_name():
    import inspect
    src = inspect.getsource(app_mod)
    assert "gpt-5" not in src and "SOCIAL_LLM_MODEL" not in src


def test_plan_search_degrades_to_the_keyword_map():
    assert app_mod.plan_search("healthcare records")["how"].startswith("keyword map")

