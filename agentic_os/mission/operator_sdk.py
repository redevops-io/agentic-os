"""Operator SDK — how an agentic app becomes a capability operator (a v5 "process").

An app declares its capabilities (syscalls) once and gets, for free:
  * `GET /capabilities`  — the manifest the Mission Planner discovers + reasons over;
  * `POST /invoke`       — run a named capability with server-side idempotency dedupe
                           (exactly-once for side effects, keyed on the Idempotency-Key);
  * a `LocalOperatorClient` so the control plane can drive co-located operators in-process
    (no HTTP hop) while remote apps are driven by `HTTPOperatorClient`.

This is the seam that turns `agents/<app>` (today: an AgentConsole) into a Mission Runtime
operator without changing how the app runs standalone.
"""

from dataclasses import dataclass, field
from typing import Callable

from .types import CapabilityManifest, CapabilitySpec, NodeCost, to_jsonable

Handler = Callable[[dict], dict]


@dataclass
class Capability:
    """A capability spec bound to its handler."""
    spec: CapabilitySpec
    handler: Handler


def capability(name: str, handler: Handler, *, provides: list[str] | None = None,
               operator: str = "", inputs: dict[str, str] | None = None,
               outputs: dict[str, str] | None = None, side_effecting: bool = False,
               approval_required: bool = False, undo: str | None = None,
               permissions: list[str] | None = None, deterministic: bool = True,
               estimated_value: str = "medium", usd: float = 0.0, latency_ms: int = 0,
               required_authority: list[str] | None = None, isolation_class: str = "",
               network: list[str] | None = None,
               data_classifications: list[str] | None = None) -> Capability:
    """Ergonomic builder for one capability + its handler.

    The security fields (v0.3.x, all optional) are the metadata the opt-in Executor security seams read:
    `required_authority` feeds the delegated-authority gate (`authority_for`), `isolation_class` the
    sandbox seam (`isolation_for`), and `network`/`data_classifications` the SecurityMonitor's
    boundary telemetry. Declaring nothing leaves the capability unchanged."""
    spec = CapabilitySpec(
        name=name, operator=operator, provides=provides or [], inputs=inputs or {},
        outputs=outputs or {}, side_effecting=side_effecting, approval_required=approval_required,
        undo=undo, permissions=permissions or [], deterministic=deterministic,
        estimated_value=estimated_value, cost=NodeCost(usd=usd, latency_ms=latency_ms),
        required_authority=required_authority or [], isolation_class=isolation_class,
        network=network or [], data_classifications=data_classifications or [],
    )
    return Capability(spec=spec, handler=handler)


class Operator:
    """One app's capability operator: manifest + handlers + idempotency dedupe."""

    def __init__(self, name: str, capabilities: list[Capability]):
        self.name = name
        for c in capabilities:
            c.spec.operator = c.spec.operator or name
        self.manifest = CapabilityManifest(operator=name, capabilities=[c.spec for c in capabilities])
        self._handlers: dict[str, Handler] = {c.spec.name: c.handler for c in capabilities}
        self._seen: dict[str, dict] = {}       # idempotency_key -> first result (exactly-once)
        self.calls: list[tuple[str, str]] = []  # (capability, idempotency_key) — for assertions

    def invoke(self, capability: str, inputs: dict, idempotency_key: str = "") -> dict:
        if idempotency_key and idempotency_key in self._seen:
            return self._seen[idempotency_key]
        fn = self._handlers.get(capability)
        if fn is None:
            raise KeyError(f"operator '{self.name}' has no capability '{capability}'")
        self.calls.append((capability, idempotency_key))
        result = fn(inputs) or {}
        if idempotency_key:
            self._seen[idempotency_key] = result
        return result

    # ── HTTP surface (mount into the app's FastAPI) ──────────────────────────
    def router(self):
        from fastapi import APIRouter, Header, HTTPException
        from pydantic import BaseModel

        router = APIRouter(tags=[f"operator:{self.name}"])

        class InvokeBody(BaseModel):
            capability: str
            inputs: dict = {}
            idempotency_key: str = ""
            mission_id: str | None = None
            node_id: str | None = None

        @router.get("/capabilities")
        def capabilities():
            return to_jsonable(self.manifest)

        @router.post("/invoke")
        def invoke(body: InvokeBody, idempotency_key: str | None = Header(default=None)):
            key = body.idempotency_key or idempotency_key or ""
            try:
                return {"result": self.invoke(body.capability, body.inputs, key)}
            except KeyError as e:
                raise HTTPException(404, str(e))

        return router


class LocalOperatorClient:
    """An `OperatorClient` over in-process Operator objects — the control plane driving
    co-located operators without an HTTP hop. Remote apps use HTTPOperatorClient instead."""

    def __init__(self, operators: dict[str, Operator]):
        self._ops = operators

    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str) -> dict:
        op = self._ops.get(operator)
        if op is None:
            raise KeyError(f"no operator '{operator}' registered")
        return op.invoke(capability, inputs, idempotency_key)
