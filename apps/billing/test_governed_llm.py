"""N7: every model call in billing goes through agentic_os.app.llm.governed_text with explicit classifications and
fails closed to the deterministic text; there is no direct-HTTP model path left."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_os.governance.classification import DataClassification

HERE = Path(__file__).parent


def test_blurb_routes_through_governed_text_with_explicit_classification(load_app, monkeypatch):
    import agentic_os.app.llm as llm
    seen = {}

    def fake(prompt, *, classifications=(), **kw):
        seen["cls"], seen["prompt"] = tuple(classifications), prompt
        return "narrated"
    monkeypatch.setattr(llm, "governed_text", fake)
    app = load_app("billing")
    assert app._llm_blurb("hello") == "narrated"
    assert seen["cls"] == (DataClassification.CUSTOMER_CONFIDENTIAL,)


def test_blurb_fails_closed_when_router_refuses_or_no_endpoint(load_app, monkeypatch):
    import agentic_os.app.llm as llm

    def refuse(*a, **k):
        raise RuntimeError("RoutingRefused")
    monkeypatch.setattr(llm, "governed_text", refuse)
    app = load_app("billing")
    assert app._llm_blurb("hello") is None
    monkeypatch.undo()
    monkeypatch.delenv("REDEVOPS_LLM_BASE_URL", raising=False)
    assert load_app("billing")._llm_blurb("hello") is None          # real governed_text: no in-boundary endpoint => None


def test_assistant_qa_degrades_to_the_primer_without_a_model(load_app, monkeypatch):
    monkeypatch.delenv("REDEVOPS_LLM_BASE_URL", raising=False)
    app = load_app("billing")
    console = [v for k, v in vars(app).items() if k.endswith("_CONSOLE")][0]
    out = console.respond("what is a profile or an invoice or a journal entry")
    assert out["intent"] in ("answer",) or out["text"]


@pytest.mark.parametrize("fname", ["app.py", "core.py", "assistant.py", "operator.py"])
def test_no_direct_model_http_left(fname):
    src = (HERE / fname).read_text()
    for needle in ("/chat/completions", "api.openai.com", "api.anthropic.com", "OpenAICompatibleModel",
                   "AgentConsole", "ANTHROPIC_API_KEY"):
        assert needle not in src
