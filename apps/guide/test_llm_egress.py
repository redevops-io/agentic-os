"""N7: every model call in guide routes through agentic_os.app.llm.governed_text with an EXPLICIT
classification, fails closed to deterministic output, and has no direct-HTTP / external fallback."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentic_os.governance.classification import DataClassification as DC

CLS = DC.INTERNAL


@pytest.fixture()
def calls(monkeypatch):
    import agentic_os.app.llm as llm
    seen = []

    def fake(prompt, **kw):
        seen.append(kw)
        return "governed text"
    monkeypatch.setattr(llm, "governed_text", fake)
    return seen


def test__llm_routes_via_governed_text_with_explicit_classification(load_app, calls):
    app = load_app("guide")
    assert app._llm("p") == "governed text"
    assert calls and tuple(calls[0]["classifications"]) == (CLS,)


def test__llm_fails_closed_without_in_boundary_model(load_app, monkeypatch):
    app = load_app("guide")
    monkeypatch.delenv("REDEVOPS_LLM_BASE_URL", raising=False)
    if hasattr(app, "httpx"):
        monkeypatch.setattr(app.httpx, "post", lambda *a, **k: pytest.fail("direct model HTTP is forbidden"))
    assert app._llm("p") is None
    # a PUBLIC host is not in-boundary: still no call, still None
    monkeypatch.setenv("REDEVOPS_LLM_BASE_URL", "https://api.example.com/v1")
    assert app._llm("p") is None
