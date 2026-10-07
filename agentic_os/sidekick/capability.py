"""SidekickCapability registry — typed capability discovery (plan §4).

The plan's core architectural correction: ReDevOps should NOT ship a catalog of separate named agents
(CRM/Quote/Deal/Order/…). It ships one Sidekick that exposes reusable *capability families* over the shared
Runtime, and the Runtime discovers which capability answers an intent. Today that discovery does not exist —
behaviour is hardcoded around named apps. This module is the keystone: a typed, provider-neutral registry so a
capability becomes *discoverable and bindable* instead of a library nobody calls.

Pure contracts + a deterministic registry — no I/O, no LLM. (An LLM planner may sit ABOVE this to turn free text
into an intent, but capability selection itself stays deterministic and inspectable.) Each capability declares its
intents, inputs, provider needs, produced artifacts, candidate actions, and its authority/verification/learning
contracts, plus audit metadata (maturity L0–L5 per §27, status, code_ref). A capability may carry an optional bound
``handler``; the public kernel ships descriptors (metadata only) and a domain module or app binds the executable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple


class CapabilityDomain(str, Enum):
    """The commercial-lifecycle families (plan §2/§4)."""
    ACQUISITION = "acquisition"
    SALES = "sales"
    COMMERCIAL = "commercial"
    OPERATIONS = "operations"
    FINANCE = "finance"


class Maturity(str, Enum):
    """Capability maturity ladder (plan §27)."""
    L0_CONCEPT = "L0"
    L1_INTERNAL = "L1"
    L2_GENERALIZED = "L2"
    L3_MULTI_PROVIDER = "L3"
    L4_CONFIGURABLE = "L4"
    L5_LEARNING = "L5"


_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> Tuple[str, ...]:
    return tuple(_TOKEN.findall(text.lower()))


@dataclass(frozen=True)
class SidekickCapability:
    """A reusable business capability Sidekick can discover and (when bound) execute (plan §4).

    ``handler`` is optional and set by whoever owns the implementation — the public kernel registers metadata-only
    descriptors; an app/domain binds the executable via :meth:`with_handler` / :meth:`CapabilityRegistry.bind`.
    All collection fields are tuples so the descriptor is hashable/immutable."""
    capability_id: str                                      # e.g. "sales.deal_close"
    domain: CapabilityDomain
    summary: str = ""
    intents: Tuple[str, ...] = ()                           # NL intent phrases used for discovery
    required_inputs: Tuple[str, ...] = ()
    optional_inputs: Tuple[str, ...] = ()
    required_provider_capabilities: Tuple[str, ...] = ()    # what connected providers must offer
    produced_artifacts: Tuple[str, ...] = ()
    candidate_actions: Tuple[str, ...] = ()
    authority_requirements: Tuple[str, ...] = ()
    verification_contract: str = ""
    learning_contract: str = ""
    maturity: Maturity = Maturity.L0_CONCEPT
    status: str = "DESIGNED_ONLY"                           # audit status (see capability matrix)
    code_ref: str = ""                                      # where the implementation lives
    handler: Optional[Callable[..., Any]] = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.capability_id or "." not in self.capability_id:
            raise ValueError(f"capability_id must be 'domain.name', got {self.capability_id!r}")
        head = self.capability_id.split(".", 1)[0]
        if head != self.domain.value:
            raise ValueError(f"capability_id {self.capability_id!r} does not match domain {self.domain.value!r}")

    @property
    def bound(self) -> bool:
        return self.handler is not None

    def with_handler(self, handler: Callable[..., Any]) -> "SidekickCapability":
        """Return a copy with an executable bound — descriptors stay immutable."""
        return replace(self, handler=handler)

    def _haystack(self) -> Tuple[str, ...]:
        parts = [self.capability_id.replace(".", " "), self.summary, " ".join(self.intents),
                 " ".join(self.candidate_actions), self.domain.value]
        return _tokens(" ".join(parts))


@dataclass(frozen=True)
class CapabilityMatch:
    """A ranked discovery hit."""
    capability: SidekickCapability
    score: float
    missing_inputs: Tuple[str, ...] = ()
    reason: str = ""


class CapabilityRegistry:
    """Deterministic register / get / discover. Discovery ranks by intent-token overlap and (when the caller
    states what inputs it can supply) input coverage — never randomness, so the same query always ranks the same
    way and the choice is explainable."""

    def __init__(self) -> None:
        self._caps: Dict[str, SidekickCapability] = {}

    def register(self, cap: SidekickCapability, *, replace: bool = False) -> SidekickCapability:
        if cap.capability_id in self._caps and not replace:
            raise ValueError(f"capability already registered: {cap.capability_id} (pass replace=True)")
        self._caps[cap.capability_id] = cap
        return cap

    def bind(self, capability_id: str, handler: Callable[..., Any]) -> SidekickCapability:
        """Attach an executable to a previously-registered descriptor."""
        cap = self.require(capability_id)
        bound = cap.with_handler(handler)
        self._caps[capability_id] = bound
        return bound

    def get(self, capability_id: str) -> Optional[SidekickCapability]:
        return self._caps.get(capability_id)

    def require(self, capability_id: str) -> SidekickCapability:
        cap = self._caps.get(capability_id)
        if cap is None:
            raise KeyError(f"no such capability: {capability_id}")
        return cap

    def all(self) -> Tuple[SidekickCapability, ...]:
        return tuple(self._caps[k] for k in sorted(self._caps))

    def by_domain(self, domain: CapabilityDomain) -> Tuple[SidekickCapability, ...]:
        return tuple(c for c in self.all() if c.domain == domain)

    def discover(self, *, intent: str = "", domain: Optional[CapabilityDomain] = None,
                 available_inputs: Sequence[str] = (), limit: int = 0) -> Tuple[CapabilityMatch, ...]:
        """Rank capabilities for an intent. With no intent, returns every capability (optionally domain-filtered)
        scored purely on input coverage. Deterministic: ties break on capability_id."""
        avail = set(available_inputs)
        q = _tokens(intent)
        candidates = self.by_domain(domain) if domain is not None else self.all()
        matches = []
        for cap in candidates:
            missing = tuple(r for r in cap.required_inputs if r not in avail) if avail else cap.required_inputs
            coverage = 1.0 if not cap.required_inputs else 1.0 - len(missing) / len(cap.required_inputs)
            if q:
                hay = set(cap._haystack())
                hits = sum(1 for t in q if t in hay)
                intent_score = hits / len(q)
                phrase_boost = 0.15 if any(p and p.lower() in intent.lower() for p in cap.intents) else 0.0
                score = min(1.0, 0.7 * intent_score + 0.15 * coverage + phrase_boost)
                if score <= 0.0:
                    continue
                reason = f"intent {hits}/{len(q)} tokens" + (f", {len(missing)} input(s) missing" if missing else "")
            else:
                score = coverage
                reason = "all inputs present" if not missing else f"{len(missing)} input(s) missing"
            matches.append(CapabilityMatch(capability=cap, score=round(score, 4),
                                           missing_inputs=missing if avail else (), reason=reason))
        matches.sort(key=lambda m: (-m.score, m.capability.capability_id))
        return tuple(matches[:limit]) if limit else tuple(matches)


# Process-wide default registry. `catalog.register_builtin_capabilities(default_registry)` seeds it with the
# audited capabilities; apps/domains bind handlers onto those descriptors.
default_registry = CapabilityRegistry()


def register_capability(cap: SidekickCapability, *, replace: bool = False) -> SidekickCapability:
    return default_registry.register(cap, replace=replace)


__all__ = [
    "CapabilityDomain", "Maturity", "SidekickCapability", "CapabilityMatch", "CapabilityRegistry",
    "default_registry", "register_capability",
]
