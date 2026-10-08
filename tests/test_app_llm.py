"""Governed app.llm client: routes through GovernedModelRouter, fails closed, emits receipts."""
from __future__ import annotations

import pytest

from agentic_os.app.llm import GovernedLLM
from agentic_os.governance.classification import DataClassification
from agentic_os.governance.policy import PrivacyMode
from agentic_os.governance.routing import (
    ExecutionBoundary,
    GovernedModelRouter,
    ModelEndpoint,
    RoutingRefused,
    TaskClass,
)


def _in_boundary(model="local-qwen"):
    return ModelEndpoint(model_id=model, provider="selfhost", boundary=ExecutionBoundary.IN_BOUNDARY,
                         accepts=DataClassification.SECRET, deployment="proxmox")


def _external(model="frontier"):
    return ModelEndpoint(model_id=model, provider="anthropic", boundary=ExecutionBoundary.EXTERNAL,
                         accepts=DataClassification.SECRET, external_data_processor=True)


class EchoTransport:
    def __init__(self):
        self.calls = []

    def generate(self, endpoint, prompt, **kw):
        self.calls.append((endpoint.model_id, prompt))
        return f"[{endpoint.model_id}] {prompt}"


# ── private data routes in-boundary; nothing crosses ─────────────────────────

def test_private_data_routes_in_boundary():
    router = GovernedModelRouter([_in_boundary()], mode=PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST)
    llm = GovernedLLM(router, transport=EchoTransport())
    res = llm.complete("summarise the account", classifications=(DataClassification.CUSTOMER_RESTRICTED,))
    assert res.endpoint.boundary is ExecutionBoundary.IN_BOUNDARY
    assert res.receipt.crossed_boundary is False
    assert res.privacy_preserved is True
    assert res.text.startswith("[local-qwen]")


# ── fail closed: unclassified evidence is treated as confidential, not public ─

def test_unclassified_fails_closed_to_confidential():
    # Only an EXTERNAL endpoint, engineering-assist mode. Unclassified -> CUSTOMER_CONFIDENTIAL,
    # which may NOT leave the boundary -> no compliant route -> RoutingRefused.
    router = GovernedModelRouter([_external()], mode=PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST)
    llm = GovernedLLM(router)
    with pytest.raises(RoutingRefused):
        llm.route_only("do a thing")  # no classifications given


def test_public_qa_may_use_external_in_engineering_assist():
    router = GovernedModelRouter([_external()], mode=PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST)
    llm = GovernedLLM(router, transport=EchoTransport())
    res = llm.complete("public doc question", task_class=TaskClass.PUBLIC_QA,
                       classifications=(DataClassification.PUBLIC,))
    assert res.endpoint.boundary is ExecutionBoundary.EXTERNAL
    assert res.receipt.crossed_boundary is False     # PUBLIC is externally shareable
    assert res.privacy_preserved is True


# ── STRICT: external never used, even for public ─────────────────────────────

def test_strict_private_refuses_external_entirely():
    router = GovernedModelRouter([_external()], mode=PrivacyMode.STRICT_PRIVATE)
    llm = GovernedLLM(router)
    with pytest.raises(RoutingRefused):
        llm.route_only("anything", classifications=(DataClassification.PUBLIC,))


# ── OPEN: private data may cross, and the receipt says so honestly ───────────

def test_open_mode_crosses_boundary_and_receipt_records_it():
    router = GovernedModelRouter([_external()], mode=PrivacyMode.OPEN)
    llm = GovernedLLM(router, transport=EchoTransport())
    res = llm.complete("summarise the account", classifications=(DataClassification.CUSTOMER_RESTRICTED,))
    assert res.endpoint.boundary is ExecutionBoundary.EXTERNAL
    assert res.receipt.crossed_boundary is True       # private data left the boundary
    assert res.privacy_preserved is False
    assert "NOT guaranteed" in res.routing_reason


# ── from_env honours REDEVOPS_PRIVACY_MODE ───────────────────────────────────

def test_from_env_reads_privacy_mode(monkeypatch):
    monkeypatch.setenv("REDEVOPS_PRIVACY_MODE", "strict_private")
    llm = GovernedLLM.from_env((_external(),))
    assert llm.mode is PrivacyMode.STRICT_PRIVATE

    monkeypatch.setenv("REDEVOPS_PRIVACY_MODE", "open")
    assert GovernedLLM.from_env().mode is PrivacyMode.OPEN

    monkeypatch.delenv("REDEVOPS_PRIVACY_MODE", raising=False)
    assert GovernedLLM.from_env().mode is PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST  # safe default


# ── route_only does not invoke the transport ─────────────────────────────────

def test_route_only_does_not_generate():
    t = EchoTransport()
    router = GovernedModelRouter([_in_boundary()])
    llm = GovernedLLM(router, transport=t)
    res = llm.route_only("x", classifications=(DataClassification.INTERNAL,))
    assert res.text == ""
    assert t.calls == []


def test_governed_text_degrades_to_none_without_a_model(monkeypatch):
    """governed_text never falls back to an external provider: with no in-boundary model configured it
    returns None (the app degrades), and default_in_boundary_endpoint is None."""
    from agentic_os.app.llm import governed_text, default_in_boundary_endpoint
    monkeypatch.delenv("REDEVOPS_LLM_BASE_URL", raising=False)
    assert default_in_boundary_endpoint() is None
    assert governed_text("summarize this account") is None


def test_governed_text_builds_in_boundary_endpoint(monkeypatch):
    from agentic_os.app.llm import default_in_boundary_endpoint
    from agentic_os.governance.routing import ExecutionBoundary
    monkeypatch.setenv("REDEVOPS_LLM_BASE_URL", "http://model.internal/v1")
    ep = default_in_boundary_endpoint()
    assert ep is not None and ep.boundary is ExecutionBoundary.IN_BOUNDARY
    assert ep.network_route == "http://model.internal/v1"
