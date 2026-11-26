"""The integrated products' tools (Edge Sentinel, redevops-qa) as gateway capabilities for external agents.

The product layer already says what each tool may do (``products/schemas.TOOL_CLASS``) and validates every input against
a closed schema before any call. This maps those classes onto the gateway, adding nothing a tool cannot already do:

| Tool class | Grant (named, never default) | At the gateway |
|---|---|---|
| ``read``, ``present`` | ``security.read`` / ``quality.read`` | a read |
| ``stage`` | ``security.propose`` | a bounded write: a PROPOSAL that does nothing until a person decides it |
| ``bounded_write`` | ``quality.run`` | a bounded write: start or cancel a test run; the QA service holds any approval a run needs |

DECIDE and EXECUTE are not tools (``FORBIDDEN_TOOLS``) and are not here: a person decides and executes in Projects, where
the click mints the tenant-bound, single-use approval the product verifies. The agent then reads the result back.

A deployment with tenants limits each product to the tenants granted it (``products:<id>`` or ``products:*``). A write
needs an ``idempotency_key``, so a retry never repeats it. The product sees the agent's own identity as the actor.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from .contracts import ApprovalPolicy, CapabilityKind, CapabilityManifest, DataClass, GatewayPrincipal, RiskTier
from .registry import CapabilityRegistry, HandlerResult

#: the grants these capabilities use, with the words a person consents to
GRANTS: Dict[str, str] = {
    "security.read": "Read Edge Sentinel security posture, cases, proposed actions and their verification",
    "security.propose": "Propose blocking or unblocking an address in Edge Sentinel. Nothing happens until a person approves and executes it",
    "quality.read": "Read quality test targets, runs, results and evidence",
    "quality.run": "Start and cancel quality test runs. A run that needs approval waits for a person",
}
_OUTPUT_REFUSALS = ("unavailable", "refused", "invalid")


def _grants_for(tool: str) -> Tuple[str, ...]:
    if tool == "findings.consolidate":                       # it reads both products
        return ("security.read", "quality.read")
    from agentic_os.products.schemas import TOOL_CLASS  # noqa: PLC0415
    domain, cls = tool.split(".", 1)[0], TOOL_CLASS[tool]
    if cls in ("read", "present"):
        return (f"{domain}.read",)
    if cls == "stage" and domain == "security":
        return ("security.propose",)
    if cls == "bounded_write" and domain == "quality":
        return ("quality.run",)
    raise ValueError(f"no gateway grant for tool {tool!r} of class {cls!r}")


def manifests() -> Tuple[CapabilityManifest, ...]:
    # imported here: the products package needs an HTTP client, which a base install of the kernel does not have, and
    # this module's grant names are read there too
    from agentic_os.products.schemas import FORBIDDEN_TOOLS, INPUT_SCHEMAS, TOOL_CLASS  # noqa: PLC0415
    from agentic_os.products.tools import _TOOLS  # noqa: PLC0415
    out = []
    for tool, _domain, summary, *_rest in _TOOLS:
        if tool in FORBIDDEN_TOOLS:                          # never, whatever the table above says
            continue
        writes = TOOL_CLASS[tool] in ("stage", "bounded_write")
        schema = {**INPUT_SCHEMAS[tool], "properties": dict(INPUT_SCHEMAS[tool].get("properties", {})),
                  "required": list(INPUT_SCHEMAS[tool].get("required", []))}
        if writes:
            schema["properties"]["idempotency_key"] = {"type": "string", "minLength": 8, "maxLength": 128,
                                                       "description": "choose one per intended action; a retry with the same key never repeats it"}
            schema["required"].append("idempotency_key")
        out.append(CapabilityManifest(
            tool, summary, CapabilityKind.DIRECT, permissions=_grants_for(tool),
            risk_tier=RiskTier.BOUNDED_WRITE if writes else RiskTier.READ, side_effecting=writes,
            # the gateway adds no second approval: what needs a person (deciding, executing, a gated run) is held by the product
            approval_policy=ApprovalPolicy.AUTO, idempotent=writes, provider=tool.split(".", 1)[0] if writes else "",
            input_schema=schema, data_classes=(DataClass.INTERNAL,)))
    return tuple(out)


def _tool_class(tool: str) -> str:
    from agentic_os.products.schemas import TOOL_CLASS  # noqa: PLC0415
    return TOOL_CLASS.get(tool, "")


def tenant_may_use(principal: GatewayPrincipal, product_id: str) -> bool:
    """On a deployment with tenants: whether the caller's tenant is granted this product. Fail closed."""
    return tenant_has_product(principal.tenant, product_id)


def tenant_has_product(tenant: str, product_id: str) -> bool:
    """The same question about a tenant, whoever is asking (an agent, or a person deciding)."""
    import os  # noqa: PLC0415

    from agentic_os.mission.control_auth import ENV_TENANT_LIMITS, ConfigError, tenant_limits  # noqa: PLC0415

    from .scope import tenant_is_current  # noqa: PLC0415
    if not os.environ.get(ENV_TENANT_LIMITS, "").strip():
        return True
    if not tenant_is_current(tenant):
        return False
    try:
        refs, _ = tenant_limits(tenant)
    except ConfigError:
        return False
    return "products:*" in refs or f"products:{product_id}" in refs


def register(registry: CapabilityRegistry, tools: Any, *, proposals: Optional[Any] = None,
             approval_url: Optional[Any] = None) -> CapabilityRegistry:
    """``tools`` is a ``products.tools.ProductTools`` bound to the deployment's product hub. ``proposals`` records which
    agent made each proposal or run; ``approval_url(product, record)`` is the deep link returned for what awaits a person."""
    from .capabilities import mask_emails  # noqa: PLC0415

    def handler(name: str):
        def run(req, envelope) -> HandlerResult:
            product = tools.product_for(name)
            if product is not None and not tenant_may_use(req.principal, product):
                return HandlerResult(ok=False, error="not permitted: your tenant is not granted this product")
            inputs = {k: v for k, v in dict(req.arguments).items() if k != "idempotency_key"}
            result = tools.call(name, inputs, actor=req.principal.principal.id)
            data: Mapping[str, Any] = result.data or {}
            if data.get("status") in _OUTPUT_REFUSALS:          # never an empty success: say what the product said
                return HandlerResult(ok=False, error=f"{data['status']}: {result.answer}"[:500])
            output = {"answer": result.answer, **data, "evidence": list(result.evidence or ())}
            record = str(data.get("request_id") or data.get("run_id") or "")
            if product is not None and record:
                if proposals is not None and _tool_class(name) in ("stage", "bounded_write"):
                    proposals.record(product, record, req.principal.principal.id, req.principal.tenant)
                if approval_url is not None and data.get("approval"):         # the card of something that awaits a person
                    output["approval_url"] = approval_url(product, record)
            return HandlerResult(ok=True, output=mask_emails(output), data_classes=(DataClass.INTERNAL,),
                                 evidence_refs=tuple(str(e) for e in (result.evidence or ())[:20]))
        return run

    for manifest in manifests():
        registry.register(manifest, handler(manifest.name))
    return registry


__all__ = ["GRANTS", "manifests", "register", "tenant_may_use"]
