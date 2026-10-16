"""Operator clients — how the Executor actually runs a capability.

`HTTPOperatorClient` POSTs the owning app's `/invoke` (the capability-as-syscall REST surface);
this is the direct, non-durable client. `dagster_exec.DagsterOperatorClient` wraps the same call
in a durable Dagster run. Both implement the `OperatorClient` Protocol, so the runtime is
oblivious to which one it holds. Operator name → base URL is resolved via a dict or a callable
(from modules.yaml in prod).
"""
from __future__ import annotations

from typing import Callable

import logging

from .approval import ENV_SECRET, mint_approval_token
from .util import post_json, Transport

_log = logging.getLogger(__name__)
_warned = False


def _warn_no_secret() -> None:
    global _warned
    if not _warned:
        _warned = True
        _log.warning("approved action cannot execute over HTTP without %s (set it on the runtime AND each "
                     "operator); the operator will stage it instead", ENV_SECRET)


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
        inputs = dict(inputs)
        marker = inputs.pop("_approval", None)
        node_id = inputs.pop("_node", None)
        body = {"capability": capability, "inputs": inputs,
                "mission_id": inputs.get("_mission"), "node_id": node_id,
                "idempotency_key": idempotency_key}
        mid = body["mission_id"]
        if mid is not None and not isinstance(mid, str):
            body["mission_id"] = mid = str(mid)
        if isinstance(marker, dict) and marker and (mid is None or not mid.strip()):
            # Never mint an UNBOUND-mission approval token: it would verify for any mission. Send no token, so
            # the operator stages (fail closed).
            import logging  # noqa: PLC0415
            logging.getLogger("agentic_os.operator").warning(
                "approval marker without _mission for %s/%s: not minting token (staged)", operator, capability)
        elif isinstance(marker, dict) and marker:
            # The marker must not travel in `inputs` (the operator strips it as a forgery). Send it as a signed
            # token instead; with no secret configured nothing is sent and the handler stages (fail closed).
            token = mint_approval_token(None, operator=operator, capability=capability,
                                        mission_id=body["mission_id"], node_id=node_id,
                                        inputs=inputs, marker=marker)
            if token:
                body["approval_token"] = token
            else:
                _warn_no_secret()
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        from .control_auth import configured_token  # noqa: PLC0415
        if configured_token():                      # operators require the same bearer credential as the runtime
            headers["Authorization"] = f"Bearer {configured_token()}"
        data = post_json(f"{self._base(operator)}/invoke", body, headers, self.timeout, self.transport)
        # tolerate either {"result": {...}} or a bare result object
        return data.get("result", data) if isinstance(data, dict) else {"result": data}
