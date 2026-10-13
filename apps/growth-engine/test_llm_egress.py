"""N7: every model call in growth-engine routes through agentic_os.app.llm.governed_text with an EXPLICIT
classification, fails closed to deterministic output, and has no direct-HTTP / external fallback."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentic_os.governance.classification import DataClassification as DC

CLS = DC.CUSTOMER_CONFIDENTIAL


@pytest.fixture()
def calls(monkeypatch):
    import agentic_os.app.llm as llm
    seen = []

    def fake(prompt, **kw):
        seen.append(kw)
        return "governed text"
    monkeypatch.setattr(llm, "governed_text", fake)
    return seen


def test__llm_blurb_routes_via_governed_text_with_explicit_classification(load_app, calls):
    app = load_app("growth-engine")
    assert app._llm_blurb("p") == "governed text"
    assert calls and tuple(calls[0]["classifications"]) == (CLS,)


def test__llm_blurb_fails_closed_without_in_boundary_model(load_app, monkeypatch):
    app = load_app("growth-engine")
    monkeypatch.delenv("REDEVOPS_LLM_BASE_URL", raising=False)
    if hasattr(app, "httpx"):
        monkeypatch.setattr(app.httpx, "post", lambda *a, **k: pytest.fail("direct model HTTP is forbidden"))
    assert app._llm_blurb("p") is None
    # a PUBLIC host is not in-boundary: still no call, still None
    monkeypatch.setenv("REDEVOPS_LLM_BASE_URL", "https://api.example.com/v1")
    assert app._llm_blurb("p") is None


def test_console_model_is_governed_and_raises_when_no_route(load_app, calls, monkeypatch):
    import agentic_os.app.llm as llm
    app = load_app("growth-engine")
    m = app._GovernedConsoleModel()
    req = SimpleNamespace(messages=({"role": "user", "content": "hi"},), system="sys", max_tokens=50)
    res = m.complete(req)
    assert res.text == "governed text" and tuple(calls[0]["classifications"]) == (CLS,)
    assert calls[0]["system"] == "sys"
    monkeypatch.setattr(llm, "governed_text", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        m.complete(req)
    assert m.info().name
