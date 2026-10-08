"""agentic_os.app.llm — the ONLY LLM client an app may import (plan §3.6, N7).

Every model call an app makes goes through here, so it goes through the ``GovernedModelRouter``:
the privacy mode (from ``REDEVOPS_PRIVACY_MODE``) gates whether anything may leave the trust
boundary, the route is chosen by data classification, an ``InferenceReceipt`` is produced for every
call, and there is no code path to ``api.anthropic.com`` / raw httpx. This is what makes
``REDEVOPS_PRIVACY_MODE`` real instead of a label.

Fail-closed on unclassified evidence: the kernel's ``DataClassification`` has no ``UNCLASSIFIED``
member and its lowest tier is ``PUBLIC`` — which would be the *most* permissive routing. So empty
classifications are treated here as ``unclassified_as`` (default ``CUSTOMER_CONFIDENTIAL``): a call
whose evidence was never classified is routed as if it were confidential, never as public (plan
§3.6 / §10.1). No compliant route -> ``RoutingRefused`` propagates (the router never silently falls
back to public inference).

The actual inference is a provider-specific ``Transport`` injected by the deployment; the kernel
ships the governance, not a vendor SDK. With no transport the client is route-only (decision +
receipt), which is what most governance tests and dry runs need.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, Tuple

from agentic_os.governance.classification import DataClassification
from agentic_os.governance.policy import PrivacyMode, privacy_mode_from_env
from agentic_os.governance.receipt import InferenceReceipt, receipt_for
from agentic_os.governance.routing import (
    GovernedModelRouter,
    ModelEndpoint,
    ModelRequest,
    TaskClass,
)

APP_LLM_CONTRACT_VERSION = "app-llm/v1"

# An app whose evidence was never classified is routed as if confidential — never public.
DEFAULT_UNCLASSIFIED_AS = DataClassification.CUSTOMER_CONFIDENTIAL


class Transport(Protocol):
    """The provider-specific inference call, injected by the deployment. It receives the ALREADY
    ROUTED, approved endpoint — it must never pick its own."""

    def generate(self, endpoint: ModelEndpoint, prompt: str, **kwargs: Any) -> str: ...


@dataclass(frozen=True)
class LLMResult:
    text: str
    endpoint: ModelEndpoint
    receipt: InferenceReceipt
    maximum_classification: DataClassification
    privacy_preserved: bool
    routing_reason: str


class GovernedLLM:
    """The governed LLM client. Construct from an explicit router, or :meth:`from_env` to pick the
    privacy mode up from the environment."""

    def __init__(self, router: GovernedModelRouter, *, transport: Optional[Transport] = None,
                 unclassified_as: DataClassification = DEFAULT_UNCLASSIFIED_AS) -> None:
        self._router = router
        self._transport = transport
        self._unclassified_as = unclassified_as

    @classmethod
    def from_env(cls, endpoints: Tuple[ModelEndpoint, ...] = (), *,
                 transport: Optional[Transport] = None,
                 unclassified_as: DataClassification = DEFAULT_UNCLASSIFIED_AS) -> "GovernedLLM":
        router = GovernedModelRouter(endpoints, mode=privacy_mode_from_env())
        return cls(router, transport=transport, unclassified_as=unclassified_as)

    @property
    def mode(self) -> PrivacyMode:
        return self._router.mode

    def _request(self, prompt: str, *, task_class: TaskClass,
                 classifications: Tuple[DataClassification, ...],
                 required_capabilities: Tuple[str, ...], tenant: str,
                 evidence_refs: Tuple[str, ...]) -> ModelRequest:
        effective = classifications or (self._unclassified_as,)   # fail closed on unclassified
        return ModelRequest(
            task=(prompt or "")[:120], task_class=task_class, classifications=tuple(effective),
            required_capabilities=tuple(required_capabilities), tenant=tenant,
            evidence_refs=tuple(evidence_refs),
        )

    def route_only(self, prompt: str, *, task_class: TaskClass = TaskClass.BUSINESS_REASONING,
                   classifications: Tuple[DataClassification, ...] = (),
                   required_capabilities: Tuple[str, ...] = (), tenant: str = "",
                   evidence_refs: Tuple[str, ...] = (),
                   tool_calls: Tuple[str, ...] = ()) -> LLMResult:
        """Resolve the route + receipt WITHOUT generating. Raises ``RoutingRefused`` (fail closed)
        when no compliant endpoint exists."""
        req = self._request(prompt, task_class=task_class, classifications=classifications,
                            required_capabilities=required_capabilities, tenant=tenant,
                            evidence_refs=evidence_refs)
        decision = self._router.route(req)
        if not decision.permitted or decision.endpoint is None:
            # mirror require_route's fail-closed contract
            self._router.require_route(req)  # raises RoutingRefused
        endpoint = decision.endpoint
        receipt = receipt_for(endpoint, maximum_classification=req.max_classification,
                              evidence_refs=req.evidence_refs, tool_calls=tool_calls)
        return LLMResult(text="", endpoint=endpoint, receipt=receipt,
                         maximum_classification=req.max_classification,
                         privacy_preserved=decision.privacy_preserved, routing_reason=decision.reason)

    def complete(self, prompt: str, *, task_class: TaskClass = TaskClass.BUSINESS_REASONING,
                 classifications: Tuple[DataClassification, ...] = (),
                 required_capabilities: Tuple[str, ...] = (), tenant: str = "",
                 evidence_refs: Tuple[str, ...] = (), tool_calls: Tuple[str, ...] = (),
                 **transport_kwargs: Any) -> LLMResult:
        """Route (fail-closed) then generate via the injected transport. With no transport this is
        route-only and ``text`` is empty."""
        routed = self.route_only(prompt, task_class=task_class, classifications=classifications,
                                 required_capabilities=required_capabilities, tenant=tenant,
                                 evidence_refs=evidence_refs, tool_calls=tool_calls)
        text = ""
        if self._transport is not None:
            text = self._transport.generate(routed.endpoint, prompt, **transport_kwargs)
        return LLMResult(text=text, endpoint=routed.endpoint, receipt=routed.receipt,
                         maximum_classification=routed.maximum_classification,
                         privacy_preserved=routed.privacy_preserved,
                         routing_reason=routed.routing_reason)


def default_in_boundary_endpoint() -> "ModelEndpoint | None":
    """The app's self-hosted in-boundary model, from ``REDEVOPS_LLM_BASE_URL`` — or ``None`` when unset.
    Declaring it IN_BOUNDARY means anything it receives stays inside the boundary. Returns ``None`` when no
    model is configured, so a caller degrades (no inference) instead of reaching an external provider."""
    import os
    from agentic_os.governance.routing import ExecutionBoundary
    base = os.environ.get("REDEVOPS_LLM_BASE_URL", "") or ""
    if not base:
        return None
    return ModelEndpoint(model_id=os.environ.get("REDEVOPS_LLM_MODEL", "DeepSeek-V4-Flash"),
                         provider="self-hosted", boundary=ExecutionBoundary.IN_BOUNDARY,
                         accepts=DataClassification.SECRET, network_route=base)


def governed_text(prompt: str, *, classifications: Tuple[DataClassification, ...] = (),
                  task_class: TaskClass = TaskClass.BUSINESS_REASONING,
                  max_tokens: int = 900, temperature: float = 0.5, system: str = "") -> "str | None":
    """One-call governed inference for an app (the N7 replacement for an ad-hoc ``_llm_*`` helper that
    posted to api.anthropic.com on its own): route the prompt through the GovernedLLM to the configured
    in-boundary model and return the text, or ``None`` if no compliant route exists — fail-closed, so the
    app degrades and NEVER silently falls back to an external provider.

    Business/customer evidence is ``CUSTOMER_CONFIDENTIAL`` by default, so in strict-private mode it can
    only reach the in-boundary endpoint; an external route is refused.
    """
    from agentic_os.app.transports import OpenAICompatibleTransport
    ep = default_in_boundary_endpoint()
    if ep is None:
        return None
    gov = GovernedLLM.from_env(endpoints=(ep,), transport=OpenAICompatibleTransport.from_env())
    try:
        res = gov.complete(prompt, task_class=task_class,
                           classifications=classifications or (DEFAULT_UNCLASSIFIED_AS,),
                           system=system, max_tokens=max_tokens, temperature=temperature)
    except Exception:
        return None       # RoutingRefused (no compliant endpoint) / transport error → degrade, don't leak
    txt = (res.text or "").strip()
    return txt or None


__all__ = [
    "APP_LLM_CONTRACT_VERSION",
    "DEFAULT_UNCLASSIFIED_AS",
    "Transport",
    "LLMResult",
    "GovernedLLM",
    "default_in_boundary_endpoint",
    "governed_text",
]
