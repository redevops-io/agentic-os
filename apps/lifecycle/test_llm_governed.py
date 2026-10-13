"""N7: every model call of lifecycle routes through agentic_os.app.llm.governed_text as CUSTOMER_CONFIDENTIAL and fails
closed (no in-boundary endpoint / router refusal => deterministic text), never to a direct HTTP client."""
from __future__ import annotations

import importlib

import pytest

app_mod = importlib.import_module("lifecycle.app")


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


def test_compose_without_a_model_and_without_body_refuses_to_create_an_empty_campaign():
    res = app_mod._compose_campaign({"prompt": "welcome"})
    assert res["status"] == "llm_unavailable" and res["campaign_id"] is None

