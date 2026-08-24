"""Opt-in isolation seam: a capability that DECLARES an isolation class runs confined through the injected
sandbox, and — fail-closed — cannot run in-process if no sandbox is wired. Capabilities that declare no
isolation, and the default executor, are unchanged."""
from __future__ import annotations

import pytest

from agentic_os.mission.executor import Executor, InMemoryOperatorClient, OperatorError
from agentic_os.mission.types import Node


class RecordingSandbox:
    def __init__(self):
        self.confined: list[tuple[str, str]] = []

    def invoke(self, operator, capability, inputs, idempotency_key, *, isolation):
        self.confined.append((capability, isolation))
        return {"confined": True, "isolation": isolation}


def _client():
    return InMemoryOperatorClient({"risky": lambda i: {"ran": "in_process"},
                                   "safe": lambda i: {"ran": "in_process"}})


ISO = {"risky": "strict"}                       # only 'risky' declares isolation
def isolation_for(node: Node) -> str:
    return ISO.get(node.capability, "")


def test_declared_isolation_routes_through_the_sandbox():
    client, sbx = _client(), RecordingSandbox()
    ex = Executor(client, sandbox=sbx, isolation_for=isolation_for)
    out = ex.run(Node(capability="risky", operator="op"), {})
    assert out["confined"] is True and sbx.confined == [("risky", "strict")]
    assert client.calls == []                   # never touched the in-process client


def test_undeclared_capability_runs_in_process():
    client, sbx = _client(), RecordingSandbox()
    ex = Executor(client, sandbox=sbx, isolation_for=isolation_for)
    out = ex.run(Node(capability="safe", operator="op"), {})
    assert out["ran"] == "in_process" and sbx.confined == []


def test_required_isolation_without_a_sandbox_fails_closed():
    ex = Executor(_client(), isolation_for=isolation_for)   # isolation required, but no sandbox wired
    with pytest.raises(OperatorError, match="requires isolation"):
        ex.run(Node(capability="risky", operator="op"), {})


def test_default_executor_is_unchanged():
    client = _client()
    ex = Executor(client)                       # no sandbox, no isolation_for → historical behaviour
    out = ex.run(Node(capability="risky", operator="op"), {})
    assert out["ran"] == "in_process" and client.calls == [("risky", "")]
