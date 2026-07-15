"""Operator clients — how the Executor actually runs a capability.

`HTTPOperatorClient` POSTs the owning app's `/invoke` (the capability-as-syscall REST surface);
this is the direct, non-durable client. `dagster_exec.DagsterOperatorClient` wraps the same call
in a durable Dagster run. Both implement the `OperatorClient` Protocol, so the runtime is
oblivious to which one it holds. Operator name → base URL is resolved via a dict or a callable
(from modules.yaml in prod).
"""
from __future__ import annotations

from typing import Callable

from .util import post_json, Transport


class HTTPOperatorClient:
    """Calls `POST {operator_base}/invoke` with {capability, inputs, idempotency_key}.

    The idempotency key is also sent as an `Idempotency-Key` header so the operator can dedupe
    server-side (exactly-once for side effects). Returns the operator's `result` payload."""

    def __init__(self, resolve: dict[str, str] | Callable[[str], str],
                 timeout: float = 60.0, transport: Transport | None = None):
        self._resolve = resolve
        self.timeout = timeout
        self.transport = transport

    def _base(self, operator: str) -> str:
        base = self._resolve(operator) if callable(self._resolve) else self._resolve.get(operator)
        if not base:
            raise KeyError(f"no base URL registered for operator '{operator}'")
        return base.rstrip("/")

    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str) -> dict:
        body = {"capability": capability, "inputs": inputs,
                "mission_id": inputs.get("_mission"), "node_id": None,
                "idempotency_key": idempotency_key}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        data = post_json(f"{self._base(operator)}/invoke", body, headers, self.timeout, self.transport)
        # tolerate either {"result": {...}} or a bare result object
        return data.get("result", data) if isinstance(data, dict) else {"result": data}
