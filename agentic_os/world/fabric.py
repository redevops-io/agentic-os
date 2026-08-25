"""Capability Fabric — plan against capabilities, not vendor code (v2 §2).

A capability is a governed operation with a provider, scopes/required authority, a side-effect class,
reversibility, cost/latency/reliability, and a realism class. The fabric resolves a capability id to a
provider (allowing vendor substitution — ``send_customer_email`` → Gmail | M365 | HubSpot), checks the
mission's authority, honours the execution mode (SIMULATE routes writes to the Outcome Simulator; LIVE
requires an authorized provider), and records what it actually did — never presenting a simulated write as
live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from runtime_contracts.world import RealismClass

from .models import ExecutionMode


class SideEffect:
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"     # a write that leaves the boundary (email/booking/payment)


@dataclass
class CapabilityProvider:
    """One concrete way to satisfy a capability. ``fn(inputs, ctx) -> dict`` performs the work; for a
    write capability under SIMULATE the fabric routes to the Outcome Simulator instead of calling ``fn``."""
    capability_id: str
    provider: str                         # e.g. "twenty", "gmail", "geo-engine", "pricing"
    fn: Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]
    required_authority: tuple = ()
    side_effect: str = SideEffect.READ
    reversible: bool = True
    realism: str = RealismClass.SIMULATED.value
    cost_usd: float = 0.0
    latency_ms: int = 20
    reliability: float = 0.99


class CapabilityDenied(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Invocation:
    """The record of one governed capability execution — what ran, under what realism, at what cost."""
    capability_id: str
    provider: str
    realism: str
    side_effect: str
    result: Dict[str, Any]
    cost_usd: float
    latency_ms: int
    simulated: bool


class CapabilityFabric:
    """Registry + governed executor for capabilities. Providers register per capability id; the fabric
    picks one (first registered, or by ``prefer``), authorizes it, and executes under the mode."""

    def __init__(self, *, authority: Any = None, simulator: Any = None,
                 mode: ExecutionMode = ExecutionMode.SIMULATE) -> None:
        self._providers: Dict[str, List[CapabilityProvider]] = {}
        self.authority = authority
        self.simulator = simulator
        self.mode = mode
        self.invocations: List[Invocation] = []
        self._disabled: set = set()        # capabilities knocked out by a perturbation (capability loss)

    def register(self, p: CapabilityProvider) -> None:
        self._providers.setdefault(p.capability_id, []).append(p)

    def providers_for(self, capability_id: str) -> List[CapabilityProvider]:
        return list(self._providers.get(capability_id, []))

    def disable(self, capability_id: str) -> None:
        self._disabled.add(capability_id)

    def _authorized(self, p: CapabilityProvider) -> bool:
        if not p.required_authority or self.authority is None:
            return True
        permits = getattr(self.authority, "permits", None)
        return all(permits(s) for s in p.required_authority) if callable(permits) else True

    def invoke(self, capability_id: str, inputs: Dict[str, Any], *, ctx: Optional[Dict[str, Any]] = None,
               prefer: str = "") -> Invocation:
        if capability_id in self._disabled:
            raise CapabilityDenied(f"capability '{capability_id}' unavailable (perturbation: capability_loss)")
        cands = self.providers_for(capability_id)
        if not cands:
            raise CapabilityDenied(f"no provider for capability '{capability_id}'")
        chosen = next((c for c in cands if c.provider == prefer), cands[0])
        if not self._authorized(chosen):
            raise CapabilityDenied(f"authority does not cover {chosen.required_authority} for '{capability_id}'")

        simulated = False
        if chosen.side_effect in (SideEffect.WRITE, SideEffect.EXTERNAL) and self.mode != ExecutionMode.LIVE:
            # a consequential write under a non-LIVE mode is routed to the Outcome Simulator, never fired
            simulated = True
            if self.simulator is not None:
                result = self.simulator.record(capability_id, inputs, provider=chosen.provider)
            else:
                result = {"simulated": True, "capability": capability_id}
        else:
            result = chosen.fn(inputs, ctx or {}) or {}

        realism = RealismClass.SIMULATED.value if simulated else chosen.realism
        inv = Invocation(capability_id=capability_id, provider=chosen.provider, realism=realism,
                         side_effect=chosen.side_effect, result=result, cost_usd=chosen.cost_usd,
                         latency_ms=chosen.latency_ms, simulated=simulated)
        self.invocations.append(inv)
        return inv

    @property
    def total_cost(self) -> float:
        return round(sum(i.cost_usd for i in self.invocations), 4)

    @property
    def total_latency_ms(self) -> int:
        return sum(i.latency_ms for i in self.invocations)
