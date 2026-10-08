"""OpenAICompatibleTransport: calls the ROUTED endpoint's route, end-to-end through GovernedLLM."""
from __future__ import annotations

import pytest

from agentic_os.app.llm import GovernedLLM
from agentic_os.app.transports import OpenAICompatibleTransport
from agentic_os.governance.classification import DataClassification as DC
from agentic_os.governance.policy import PrivacyMode
from agentic_os.governance.routing import ExecutionBoundary, GovernedModelRouter, ModelEndpoint


class _FakeResp:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_transport_posts_to_the_routed_endpoints_network_route(monkeypatch):
    calls = {}

    def fake_post(url, json, headers, timeout):
        calls["url"] = url
        calls["model"] = json["model"]
        calls["auth"] = headers["Authorization"]
        return _FakeResp("hello from " + json["model"])

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    endpoint = ModelEndpoint(model_id="qwen", provider="selfhost",
                             boundary=ExecutionBoundary.IN_BOUNDARY, accepts=DC.SECRET,
                             network_route="http://evo-x2:8000/v1")
    llm = GovernedLLM(GovernedModelRouter([endpoint], mode=PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST),
                      transport=OpenAICompatibleTransport(api_key="k"))
    res = llm.complete("hi", classifications=(DC.CUSTOMER_RESTRICTED,))

    assert res.text == "hello from qwen"
    assert calls["url"] == "http://evo-x2:8000/v1/chat/completions"   # the routed endpoint's route
    assert calls["model"] == "qwen"
    assert calls["auth"] == "Bearer k"
    assert res.receipt.crossed_boundary is False


def test_from_env_reads_base_url(monkeypatch):
    monkeypatch.setenv("REDEVOPS_LLM_BASE_URL", "http://host:9000/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sek")
    t = OpenAICompatibleTransport.from_env()
    assert t.base_url == "http://host:9000/v1"
    assert t.api_key == "sek"


def test_missing_base_url_raises():
    t = OpenAICompatibleTransport()   # no base_url
    ep = ModelEndpoint(model_id="m", provider="p", boundary=ExecutionBoundary.IN_BOUNDARY)
    with pytest.raises(RuntimeError, match="no base URL"):
        t.generate(ep, "x")
