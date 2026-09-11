"""Policy-scoped capability registry (plan §3.3).

The fleet may declare many capabilities; a given principal must see only the subset its
identity + scopes permit. The agent-visible list (and therefore the MCP tool list) is *derived*
from this filter — the plan's invariant that a hidden capability can be neither enumerated nor
invoked (plan §13 Phase-1 acceptance, §18 non-goals) lives here and in the pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from agentic_os.overlays import Principal, identity_provider

from .contracts import CapabilityManifest, GatewayPrincipal, GatewayRequest


@dataclass(frozen=True)
class HandlerResult:
    """What a DIRECT capability handler returns to the pipeline (before egress filtering)."""
    ok: bool
    output: object = None
    data_classes: Tuple = ()
    source_refs: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    error: str = ""


class CapabilityHandler(Protocol):
    """Fulfils a DIRECT capability. Receives the request and the GovernedEnvelope built for it
    (``None`` for reads). MUST NOT bypass governance — it is only reached after the pipeline's
    permission/risk/approval/egress decisions. Should raise nothing user-facing; return an error."""

    def __call__(self, request: GatewayRequest, envelope: object) -> HandlerResult: ...


# authorize(principal, permission) -> bool ; defaults to the process identity plane (deny-by-default)
Authorizer = Callable[[Principal, str], bool]


def _default_authorizer(principal: Principal, permission: str) -> bool:
    return identity_provider().authorize(principal, permission)


@dataclass
class CapabilityRegistry:
    """Registered capabilities + their DIRECT handlers. MISSION capabilities need no handler
    (the pipeline routes them to the Mission Runtime)."""

    _manifests: Dict[str, CapabilityManifest] = field(default_factory=dict)
    _handlers: Dict[str, CapabilityHandler] = field(default_factory=dict)

    def register(self, manifest: CapabilityManifest,
                 handler: Optional[CapabilityHandler] = None) -> "CapabilityRegistry":
        from .contracts import CapabilityKind
        if manifest.name in self._manifests:
            raise ValueError(f"capability already registered: {manifest.name}")
        if manifest.kind is CapabilityKind.DIRECT and handler is None:
            raise ValueError(f"DIRECT capability {manifest.name} requires a handler")
        if manifest.kind is CapabilityKind.MISSION and handler is not None:
            raise ValueError(f"MISSION capability {manifest.name} must not carry a handler "
                             "(it is routed to the Mission Runtime)")
        self._manifests[manifest.name] = manifest
        if handler is not None:
            self._handlers[manifest.name] = handler
        return self

    def get(self, name: str) -> Optional[CapabilityManifest]:
        return self._manifests.get(name)

    def handler(self, name: str) -> Optional[CapabilityHandler]:
        return self._handlers.get(name)

    def all(self) -> Tuple[CapabilityManifest, ...]:
        return tuple(self._manifests.values())

    # ── the per-principal filter ────────────────────────────────────────────────
    def _permitted(self, m: CapabilityManifest, gp: GatewayPrincipal,
                   authorize: Authorizer) -> bool:
        # 1. OAuth scopes: the token must carry every scope the capability declares.
        if not set(m.scopes).issubset(set(gp.scopes)):
            return False
        # 2. permissions: the identity plane must grant every required permission (deny-by-default).
        return all(authorize(gp.principal, perm) for perm in m.permissions)

    def visible_for(self, gp: GatewayPrincipal,
                    authorize: Optional[Authorizer] = None) -> List[CapabilityManifest]:
        """The capabilities this principal may see — the source of the MCP tool list."""
        auth = authorize or _default_authorizer
        return [m for m in self._manifests.values() if self._permitted(m, gp, auth)]

    def is_visible(self, name: str, gp: GatewayPrincipal,
                   authorize: Optional[Authorizer] = None) -> bool:
        m = self._manifests.get(name)
        if m is None:
            return False
        return self._permitted(m, gp, authorize or _default_authorizer)
