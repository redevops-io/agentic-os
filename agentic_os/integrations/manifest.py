"""The Integration Capability Manifest — what can actually run, per provider x capability.

Mirrors RAAAL's ``capability.py``: one :class:`CapabilityDimension` per
``(capability, provider)`` at a :class:`Support` level, and a ``decide`` / ``refusals_for``
pair that names what cannot run — never nearest-runnable. It is consulted twice: to
*ground* a proposal (only suggest what is buildable, so a user's confirmation is never
spent on an impossible plan) and as the *post-confirmation safety net*. It is never a
gate on the user's language, and its vocabulary never reaches the small-business UI.

Refusals reuse ``runtime_contracts``' :class:`CapabilityRefusal` / :class:`RefusalKind`,
so a refusal here is the same typed artifact the rest of the stack already speaks.
Deterministic and model-free.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, Optional, Tuple

from runtime_contracts import CapabilityRefusal, RefusalKind
from runtime_contracts.canonical import content_hash


class Support(str, Enum):
    """How well a ``(capability, provider)`` pair is actually supported.

    The distinction between the last two is load-bearing, exactly as in RAAAL:
    "we chose not to" (``REFUSED``) is not "we can't" (``NOT_MODELLED``)."""

    EXECUTED = "EXECUTED"          # an adapter implements it; runnable at its tier
    REFUSED = "REFUSED"            # modelled, deliberately withheld (e.g. an unofficial API)
    NOT_MODELLED = "NOT_MODELLED"  # no adapter for this pair


@dataclass(frozen=True)
class CapabilityDimension:
    """One ``(capability, provider)`` pair and how it is supported.

    ``capability`` is a logical id (``email.message.send``); ``provider`` a concrete
    system (``gmail``); ``tier`` the plan's 0-4 risk grade; ``why`` the reason a
    ``REFUSED`` / ``NOT_MODELLED`` pair cannot run; ``alternative`` an optional pointer
    to a provider that *is* runnable for the same capability.
    """

    capability: str
    provider: str
    support: Support
    tier: int = 0
    why: str = ""
    alternative: str = ""

    def __post_init__(self) -> None:
        if not 0 <= int(self.tier) <= 4:
            raise ValueError(f"tier must be 0..4, got {self.tier!r}")

    @property
    def executed(self) -> bool:
        return self.support is Support.EXECUTED

    def canonical_form(self) -> Dict[str, object]:
        return {
            "capability": self.capability,
            "provider": self.provider,
            "support": self.support.value,
            "tier": int(self.tier),
            "why": self.why,
            "alternative": self.alternative,
        }


@dataclass(frozen=True)
class IntegrationManifest:
    """The set of known ``(capability, provider)`` dimensions, content-addressed.

    ``decide`` answers "can this exact pair run?"; ``refusals_for`` answers it for a
    whole request at once, in manifest order — so a caller is told every problem
    together, not one deploy apart.
    """

    dimensions: Tuple[CapabilityDimension, ...] = ()

    # ── reality filter: what a proposal is ALLOWED to suggest ──────────────────
    def buildable(self, capability: str) -> Tuple[str, ...]:
        """Providers that actually run ``capability`` (``Support.EXECUTED``), in
        manifest order. Empty ⇒ there is no way to do this yet."""
        return tuple(
            d.provider for d in self.dimensions
            if d.capability == capability and d.support is Support.EXECUTED
        )

    def substitute(self, capability: str, *, preferred: str = "") -> Optional[str]:
        """The provider to propose for ``capability``: the caller's ``preferred`` if it
        is buildable, else the first buildable one, else ``None`` (nothing to suggest —
        keep it out of the proposal rather than promise the impossible)."""
        options = self.buildable(capability)
        if preferred and preferred in options:
            return preferred
        return options[0] if options else None

    # ── the safety net: refuse by name, never nearest-runnable ─────────────────
    def _dimension(self, capability: str, provider: str) -> Optional[CapabilityDimension]:
        for d in self.dimensions:
            if d.capability == capability and d.provider == provider:
                return d
        return None

    def decide(self, capability: str, provider: str = "") -> Optional[CapabilityRefusal]:
        """``None`` if the pair can run; otherwise a typed refusal that names the
        dimension and what *could* run instead. An empty ``provider`` asks only
        "is this capability buildable at all?"."""
        options = self.buildable(capability)
        known = any(d.capability == capability for d in self.dimensions)
        # A capability nobody implements (unknown, or modelled but every provider
        # withheld/absent) with no provider named -> NOT_MODELLED, by dimension.
        if not known or (not options and provider == ""):
            detail = (
                f"{capability!r} is only offered by: {', '.join(options)}"
                if options
                else f"no adapter implements {capability!r}"
            )
            return CapabilityRefusal(
                kind=RefusalKind.UNSUPPORTED_DIMENSION,
                dimension=capability,
                stated_value=provider or None,
                executable_values=options,
                detail=detail,
            )
        if provider == "":
            return None  # at least one provider can run it; resolution picks which
        dim = self._dimension(capability, provider)
        if dim is not None and dim.support is Support.EXECUTED:
            return None
        # provider named but modelled-and-withheld, or absent while others run it
        detail = (
            dim.why if dim is not None and dim.why
            else f"{provider!r} does not run {capability!r}"
        )
        return CapabilityRefusal(
            kind=RefusalKind.UNSUPPORTED_VALUE,
            dimension=capability,
            stated_value=provider,
            executable_values=options,
            detail=detail,
        )

    def refusals_for(
        self, needs: Iterable[Tuple[str, str]]
    ) -> Tuple[CapabilityRefusal, ...]:
        """Every refusal for a batch of ``(capability, provider)`` needs, returned
        together in manifest order — so a caller sees every gap at once. A ``provider``
        of ``""`` asks only whether the capability is buildable at all."""
        out = [
            r for r in (self.decide(cap, prov) for cap, prov in needs) if r is not None
        ]
        order = {d.capability: i for i, d in enumerate(self.dimensions)}
        out.sort(key=lambda r: (order.get(r.dimension, len(order)), str(r.stated_value or "")))
        return tuple(out)

    def canonical_form(self) -> Dict[str, object]:
        return {
            "dimensions": [
                d.canonical_form()
                for d in sorted(self.dimensions, key=lambda d: (d.capability, d.provider))
            ]
        }

    @property
    def manifest_id(self) -> str:
        return content_hash(self.canonical_form())
