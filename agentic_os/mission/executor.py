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


class Sandbox(Protocol):
    """Opt-in isolation boundary. A capability that declares an isolation class runs its invoke through
    this instead of in-process. Duck-typed so the (enterprise) sandbox implementation is injected, never
    imported here."""
    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str,
               *, isolation: str) -> dict: ...


class SecurityMonitorSpi(Protocol):
    """Opt-in security-telemetry sink. The executor calls ``observe`` at the capability boundary — outside
    the operator/model's control — so a compromised agent cannot lie by omission about what it did."""
    def observe(self, node: Node, result: dict | None, *, isolation: str, error: str | None = None) -> None: ...


class Executor:
    def __init__(self, client: OperatorClient, *, sandbox: "Sandbox | None" = None,
                 isolation_for: "Callable[[Node], str] | None" = None,
                 monitor: "SecurityMonitorSpi | None" = None):
        self.client = client
        # Opt-in isolation seam. `isolation_for(node)` reports the isolation class a capability requires
        # ("" | "in_process" | "sandbox" | "strict"), e.g. from its CapabilityDescriptor. When it requires
        # confinement, execution routes through `sandbox`. All default None → behaviour is unchanged.
        self.sandbox = sandbox
        self.isolation_for = isolation_for
        self.monitor = monitor

    def run(self, node: Node, inputs: dict) -> dict:
        node.attempts += 1
        isolation = self.isolation_for(node) if self.isolation_for else ""
        try:
            if isolation in {"sandbox", "strict"}:
                if self.sandbox is None:
                    # Fail closed: a capability that DECLARES isolation must not silently run in-process
                    # because a caller omitted the sandbox plane. Declaring isolation is the opt-in;
                    # enforcing it is not.
                    raise OperatorError(
                        f"capability '{node.capability}' requires isolation '{isolation}' but no sandbox is "
                        "wired — refusing to run it unconfined")
                result = self.sandbox.invoke(node.operator, node.capability, inputs, node.idempotency_key,
                                             isolation=isolation)
            else:
                result = self.client.invoke(node.operator, node.capability, inputs, node.idempotency_key)
        except Exception as e:
            # Emit at the boundary even on failure — a refused/failed side effect is itself telemetry.
            if self.monitor is not None:
                self.monitor.observe(node, None, isolation=isolation, error=str(e))
            raise
        if self.monitor is not None:
            self.monitor.observe(node, result, isolation=isolation)
        return result

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
