"""Executor — runs ONE node durably by calling the owning operator's capability.

In production a node becomes a Dagster op that POSTs the operator's /invoke (the reel-job
pattern generalized); here an injectable `OperatorClient` runs it in-process. Two guarantees the
executor is responsible for:

  * exactly-once side effects — retries dedupe on the node's idempotency_key (Dagster gives
    retry, not exactly-once; the operator + this cache give exactly-once).
  * sagas — a failed/rolled-back side-effecting node is compensated via its `undo` capability.
"""
from __future__ import annotations

from typing import Callable, Protocol

from .types import Node


class OperatorError(Exception):
    pass


class OperatorClient(Protocol):
    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str) -> dict: ...


class InMemoryOperatorClient:
    """Operators as in-process callables — for the demo/tests. Each handler is
    fn(inputs) -> result_dict; raise to simulate a failure. Dedupes on idempotency_key so a
    retried side-effecting call returns the first result instead of running twice."""

    def __init__(self, handlers: dict[str, Callable[[dict], dict]]):
        self._handlers = handlers
        self._seen: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []   # (capability, idempotency_key) — for assertions

    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str) -> dict:
        if idempotency_key and idempotency_key in self._seen:
            return self._seen[idempotency_key]          # exactly-once: return the prior result
        self.calls.append((capability, idempotency_key))
        fn = self._handlers.get(capability)
        if fn is None:
            raise OperatorError(f"operator '{operator}' has no handler for '{capability}'")
        result = fn(inputs) or {}
        if idempotency_key:
            self._seen[idempotency_key] = result
        return result


class Executor:
    def __init__(self, client: OperatorClient):
        self.client = client

    def run(self, node: Node, inputs: dict) -> dict:
        node.attempts += 1
        return self.client.invoke(node.operator, node.capability, inputs, node.idempotency_key)

    def compensate(self, node: Node) -> dict | None:
        """Run the node's undo capability (saga) — best effort; never raises past the caller."""
        if not node.undo:
            return None
        try:
            return self.client.invoke(node.operator, node.undo,
                                      {"undo_of": node.capability, "result": node.result},
                                      f"{node.idempotency_key}:undo")
        except Exception as e:  # noqa: BLE001
            return {"undo_error": str(e)}
