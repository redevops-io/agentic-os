"""W1 — compile a ConfirmedIntegrationIntent into a ConnectPlan.

Deterministic and model-free — RAAAL's ``from_intent.compile_intent``, retargeted. It
takes a sealed :class:`ConfirmedIntegrationIntent` and nothing else, resolves each
logical capability to a concrete provider under a fixed precedence
(**named → already-connected → workspace-preferred → manifest default → ask**), and
emits a :class:`ConnectPlan`: the ordered provider connections to make, how each
capability resolved, the defaults it applied, any refusals (the manifest safety net),
and an ordered test-mission spec. It never proposes a provider the manifest can't build,
and it refuses by name rather than silently dropping a capability.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Protocol, Tuple

from runtime_contracts import CapabilityRefusal
from runtime_contracts.canonical import content_hash

from .contracts import CapabilityRequirementGraph, ConfirmedIntegrationIntent
from .manifest import IntegrationManifest

COMPILE_VERSION = "connect-plan/0.1"

DEFAULT_AUTH = "oauth"
#: How a provider is connected — a property of the provider, not the pair.
PROVIDER_AUTH: Dict[str, str] = {
    "gmail": "oauth", "outlook": "oauth", "google_calendar": "oauth",
    "microsoft_calendar": "oauth", "slack": "oauth", "hubspot": "oauth",
    "whatsapp_business": "oauth", "stripe": "oauth", "square": "oauth",
    "quickbooks": "oauth", "xero": "oauth", "calendly": "oauth", "zoom": "oauth",
    "mailchimp": "oauth", "shopify": "oauth", "apollo": "api_key",
    "telegram": "bot_token", "discord": "bot_token", "webhook": "none",
}


class WorkspaceConnections(Protocol):
    """What the workspace already has. 'Existing-systems-win' reads this so a setup never
    asks for a provider the workspace already uses."""

    def connected(self) -> Tuple[str, ...]: ...
    def preferred(self, capability: str) -> str: ...  # "" if none


@dataclass(frozen=True)
class InMemoryConnections:
    """A plain :class:`WorkspaceConnections` for a fresh workspace / tests."""

    already: Tuple[str, ...] = ()
    preferences: Tuple[Tuple[str, str], ...] = ()  # (capability, provider)

    def connected(self) -> Tuple[str, ...]:
        return self.already

    def preferred(self, capability: str) -> str:
        for cap, prov in self.preferences:
            if cap == capability:
                return prov
        return ""


@dataclass(frozen=True)
class Resolution:
    """How one capability resolved to a provider — or why it couldn't."""

    capability: str
    provider: str  # "" if unresolved
    source: str  # named | connected | preferred | default | refused
    refusal: Optional[CapabilityRefusal] = None

    def canonical_form(self) -> Dict[str, object]:
        return {"capability": self.capability, "provider": self.provider, "source": self.source}


@dataclass(frozen=True)
class ConnectStep:
    """One provider to connect, and the capabilities it will serve once connected."""

    provider: str
    auth_method: str
    tier: int
    capabilities: Tuple[str, ...]
    already_connected: bool = False

    def canonical_form(self) -> Dict[str, object]:
        return {
            "provider": self.provider,
            "auth_method": self.auth_method,
            "tier": int(self.tier),
            "capabilities": sorted(self.capabilities),
            "already_connected": self.already_connected,
        }


@dataclass(frozen=True)
class MissionStep:
    """One ordered step of the governed test Mission — a bound (capability, provider)."""

    capability: str
    provider: str

    def canonical_form(self) -> Dict[str, object]:
        return {"capability": self.capability, "provider": self.provider}


def _refusal_json(r: CapabilityRefusal) -> Dict[str, object]:
    return {
        "kind": r.kind.value,
        "dimension": r.dimension,
        "stated_value": r.stated_value,
        "executable_values": list(r.executable_values),
        "detail": r.detail,
    }


@dataclass(frozen=True)
class ConnectPlan:
    """The compiled, content-addressed setup plan. ``connectable`` is True only when
    nothing was refused and every capability bound to a provider — the object the
    connect executor (W3) and the governed test Mission (W4) consume."""

    intent_hash: str
    resolutions: Tuple[Resolution, ...]
    connect_steps: Tuple[ConnectStep, ...]
    applied_defaults: Tuple[Tuple[str, str], ...]  # (capability, provider) filled without a user choice
    refusals: Tuple[CapabilityRefusal, ...]
    test_mission: Tuple[MissionStep, ...]
    compile_version: str = COMPILE_VERSION

    @property
    def connectable(self) -> bool:
        return not self.refusals and all(r.provider for r in self.resolutions)

    def canonical_form(self) -> Dict[str, object]:
        return {
            "compile_version": self.compile_version,
            "intent_hash": self.intent_hash,
            "resolutions": [r.canonical_form() for r in
                            sorted(self.resolutions, key=lambda r: r.capability)],
            "connect_steps": [s.canonical_form() for s in
                              sorted(self.connect_steps, key=lambda s: s.provider)],
            "applied_defaults": sorted([list(d) for d in self.applied_defaults]),
            "refusals": [_refusal_json(r) for r in self.refusals],
            "test_mission": [m.canonical_form() for m in self.test_mission],  # ordered — never sorted
        }

    @property
    def plan_id(self) -> str:
        return content_hash(self.canonical_form())


def resolve_capability(
    capability: str, *, preference: str, manifest: IntegrationManifest,
    connections: WorkspaceConnections,
) -> Resolution:
    """Resolve one capability to a provider under the fixed precedence, or refuse it by
    name if the manifest can build nothing for it."""
    options = manifest.buildable(capability)
    if not options:
        return Resolution(capability, "", "refused", manifest.decide(capability))
    if preference and preference in options:  # 1 explicitly named + buildable
        return Resolution(capability, preference, "named")
    for p in connections.connected():  # 2 already-connected + compatible
        if p in options:
            return Resolution(capability, p, "connected")
    workspace_pref = connections.preferred(capability)  # 3 workspace preferred
    if workspace_pref and workspace_pref in options:
        return Resolution(capability, workspace_pref, "preferred")
    sub = manifest.substitute(capability)  # 4 manifest/pack default (first buildable)
    if sub:
        return Resolution(capability, sub, "default")
    return Resolution(capability, "", "refused", manifest.decide(capability))


def _tier_for(manifest: IntegrationManifest, capability: str, provider: str) -> int:
    for d in manifest.dimensions:
        if d.capability == capability and d.provider == provider:
            return d.tier
    return 0


def _ordered_capabilities(graph: CapabilityRequirementGraph) -> Tuple[str, ...]:
    """Topological order honouring the graph's edges (stable Kahn); a leftover cycle
    falls back to requirement order so the test mission is always defined."""
    reqs = [r.capability for r in graph.requirements]
    incoming = {c: 0 for c in reqs}
    adj: Dict[str, List[str]] = {c: [] for c in reqs}
    for a, b in graph.edges:
        if a in adj and b in incoming:
            adj[a].append(b)
            incoming[b] += 1
    ready = [c for c in reqs if incoming[c] == 0]
    ordered: List[str] = []
    while ready:
        c = ready.pop(0)
        ordered.append(c)
        for nxt in adj[c]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
    for c in reqs:  # any leftover (cycle) in stable order
        if c not in ordered:
            ordered.append(c)
    return tuple(ordered)


def compile_integration_intent(
    intent: ConfirmedIntegrationIntent, *, manifest: IntegrationManifest,
    connections: Optional[WorkspaceConnections] = None,
    provider_auth: Optional[Mapping[str, str]] = None,
) -> ConnectPlan:
    """Compile a sealed intent into a :class:`ConnectPlan`. Takes the intent and nothing
    else (no text, no model) — the deterministic half of the wizard."""
    connections = connections or InMemoryConnections()
    auth = dict(PROVIDER_AUTH)
    if provider_auth:
        auth.update(provider_auth)
    prefs = dict(intent.provider_preferences)  # {capability: provider}

    resolutions = tuple(
        resolve_capability(
            req.capability,
            preference=prefs.get(req.capability, req.provider_preference),
            manifest=manifest, connections=connections,
        )
        for req in intent.requirements.requirements
    )
    refusals = tuple(r.refusal for r in resolutions if r.refusal is not None)
    applied_defaults = tuple((r.capability, r.provider) for r in resolutions if r.source == "default")

    connected_set = set(connections.connected())
    by_provider: Dict[str, List[Tuple[str, int]]] = {}
    for r in resolutions:
        if not r.provider:
            continue
        by_provider.setdefault(r.provider, []).append(
            (r.capability, _tier_for(manifest, r.capability, r.provider)))
    connect_steps = tuple(
        ConnectStep(
            provider=prov, auth_method=auth.get(prov, DEFAULT_AUTH),
            tier=max(t for _, t in caps), capabilities=tuple(c for c, _ in caps),
            already_connected=prov in connected_set,
        )
        for prov, caps in by_provider.items()
    )

    bound = {r.capability: r.provider for r in resolutions}
    test_mission = tuple(
        MissionStep(cap, bound.get(cap, "")) for cap in _ordered_capabilities(intent.requirements)
    )
    return ConnectPlan(
        intent_hash=intent.content_hash, resolutions=resolutions,
        connect_steps=connect_steps, applied_defaults=applied_defaults,
        refusals=refusals, test_mission=test_mission,
    )
