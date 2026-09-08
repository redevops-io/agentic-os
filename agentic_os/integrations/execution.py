"""W4 — run a ConnectPlan: connect providers, run a governed test Mission, then EXPLAIN.

Ports and adapters. This module defines the :class:`AdapterPort` it needs
(``capabilities / connect / execute / observe / health``) and drives a
:class:`~agentic_os.integrations.connect_plan.ConnectPlan` through it. Any
``redevops-connectors`` ``IntegrationAdapter`` satisfies the port *structurally*, so
agentic-os takes **no hard dependency** on the connector package — it is an optional
runtime plugin, not part of the base install (the plan's "provider dependencies install
on demand").

Every **write** step runs under a content-addressed :class:`GovernedEnvelope`; the adapter
refuses a write with no envelope present. This is the authority *carrier* — the full
membrane binding / verification plane wraps it upstream when the live path is wired; here
the run is exercised against a paper adapter with no live call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple

from runtime_contracts.canonical import content_hash

from .connect_plan import ConnectPlan


class AdapterPort(Protocol):
    """The provider surface the runner needs. Structurally identical to
    ``redevops_connectors.IntegrationAdapter`` — duck-typed so no import couples the two.
    Results are read by attribute (``.ok`` / ``.provider_object_id`` / ``.found`` /
    ``.connected``), so any shape carrying them works."""

    provider: str

    def capabilities(self) -> Tuple[Any, ...]: ...
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> Any: ...
    def execute(self, capability: str, request: Mapping[str, Any], envelope: Optional[object]) -> Any: ...
    def observe(self, resource_ref: str) -> Any: ...
    def health(self) -> Any: ...


@dataclass
class AdapterRegistry:
    """Provider id → adapter instance. Populated at runtime with real (or paper) adapters."""

    _by_provider: Dict[str, AdapterPort] = field(default_factory=dict)

    def register(self, adapter: AdapterPort) -> "AdapterRegistry":
        self._by_provider[adapter.provider] = adapter
        return self

    def get(self, provider: str) -> Optional[AdapterPort]:
        return self._by_provider.get(provider)

    def providers(self) -> Tuple[str, ...]:
        return tuple(sorted(self._by_provider))


@dataclass(frozen=True)
class GovernedEnvelope:
    """The minimal governed authority a write executes under — content-addressed. Its
    presence is what a write adapter requires; the full membrane binding is upstream."""

    intent_hash: str
    capability: str
    provider: str
    tier: int

    def canonical_form(self) -> Dict[str, object]:
        return {"intent_hash": self.intent_hash, "capability": self.capability,
                "provider": self.provider, "tier": int(self.tier)}

    @property
    def binding(self) -> str:
        return content_hash(self.canonical_form())


@dataclass(frozen=True)
class ConnectReceipt:
    provider: str
    connected: bool
    already_connected: bool
    tier: int
    detail: str = ""


@dataclass(frozen=True)
class StepRun:
    capability: str
    provider: str
    tier: int
    ok: bool
    provider_object_id: str = ""
    reconciled: Optional[bool] = None  # None ⇒ not observable
    error: str = ""


@dataclass(frozen=True)
class MissionRun:
    intent_hash: str
    steps: Tuple[StepRun, ...]

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    @property
    def reconciled(self) -> bool:
        observed = [s for s in self.steps if s.reconciled is not None]
        return bool(observed) and all(s.reconciled for s in observed)


class PlanNotConnectable(ValueError):
    """A plan with refusals or unresolved capabilities cannot be run."""


def connect_providers(
    plan: ConnectPlan, registry: AdapterRegistry, *,
    credential_refs: Optional[Mapping[str, str]] = None,
) -> Tuple[ConnectReceipt, ...]:
    """Drive ``adapter.connect`` for each ConnectStep (the one-click OAuth landing). An
    already-connected provider is a no-op; a provider with no registered adapter is
    reported unconnected rather than raising."""
    refs = dict(credential_refs or {})
    receipts = []
    for step in plan.connect_steps:
        adapter = registry.get(step.provider)
        if step.already_connected:
            receipts.append(ConnectReceipt(step.provider, True, True, step.tier, "already connected"))
            continue
        if adapter is None:
            receipts.append(ConnectReceipt(step.provider, False, False, step.tier, "no adapter registered"))
            continue
        state = adapter.connect({}, refs.get(step.provider, ""))
        receipts.append(ConnectReceipt(
            step.provider, bool(getattr(state, "connected", False)), False,
            step.tier, str(getattr(state, "detail", ""))))
    return tuple(receipts)


def _is_write(adapter: AdapterPort, capability: str) -> bool:
    for c in adapter.capabilities():
        if getattr(c, "name", "") == capability:
            return bool(getattr(c, "write", False))
    return False


def run_test_mission(
    plan: ConnectPlan, registry: AdapterRegistry, *,
    requests: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> MissionRun:
    """Run the plan's ordered test Mission through the adapters. Each write step executes
    under a :class:`GovernedEnvelope`; a successful step with a provider object id is
    reconciled by re-observing it (the 'no action is complete until observed' invariant).
    Refuses a non-connectable plan."""
    if not plan.connectable:
        raise PlanNotConnectable("plan has refusals or unresolved capabilities")
    reqs = dict(requests or {})
    tier_by = {(cap, s.provider): s.tier for s in plan.connect_steps for cap in s.capabilities}

    steps = []
    for ms in plan.test_mission:
        adapter = registry.get(ms.provider)
        tier = tier_by.get((ms.capability, ms.provider), 0)
        if adapter is None:
            steps.append(StepRun(ms.capability, ms.provider, tier, False, error="no adapter registered"))
            continue
        envelope = (GovernedEnvelope(plan.intent_hash, ms.capability, ms.provider, tier)
                    if _is_write(adapter, ms.capability) else None)
        result = adapter.execute(ms.capability, reqs.get(ms.capability, {}), envelope)
        ok = bool(getattr(result, "ok", False))
        oid = str(getattr(result, "provider_object_id", ""))
        err = str(getattr(result, "error", ""))
        reconciled: Optional[bool] = None
        if ok and oid:
            try:
                obs = adapter.observe(oid)
                reconciled = bool(getattr(obs, "found", False))
            except Exception:  # noqa: BLE001 — an adapter that can't observe leaves it UNKNOWN
                reconciled = None
        steps.append(StepRun(ms.capability, ms.provider, tier, ok, oid, reconciled, err))
    return MissionRun(plan.intent_hash, tuple(steps))


@dataclass(frozen=True)
class Explanation:
    """The EXPLAIN surface: per-step capability, provider, tier, provenance (where the
    provider choice came from), status and reconciliation — plus any refusals. This is the
    operator view; the small-business UI renders it in business language."""

    intent_hash: str
    connectable: bool
    steps: Tuple[Dict[str, Any], ...]
    refusals: Tuple[Dict[str, Any], ...]

    def to_text(self) -> str:
        lines = [f"plan {self.intent_hash[:16]}… — {'connectable' if self.connectable else 'NOT connectable'}"]
        for s in self.steps:
            recon = "" if s["reconciled"] is None else (" · reconciled" if s["reconciled"] else " · UNRECONCILED")
            lines.append(f"  {s['capability']} → {s['provider']} (tier {s['tier']}, {s['provenance']}) "
                         f"[{s['status']}]{recon}")
        for r in self.refusals:
            lines.append(f"  refused: {r['capability']} — {r['detail']}")
        return "\n".join(lines)


def explain(plan: ConnectPlan, run: Optional[MissionRun] = None) -> Explanation:
    """Explain a plan (and, if given, its run). Provenance is the resolution source
    (named/connected/preferred/default) — the 'who chose this provider' axis governance
    reads."""
    source_by = {r.capability: r.source for r in plan.resolutions}
    run_by = {(s.capability, s.provider): s for s in run.steps} if run else {}
    steps = []
    for ms in plan.test_mission:
        sr = run_by.get((ms.capability, ms.provider))
        if sr is None:
            status = "planned"
        else:
            status = "ok" if sr.ok else "failed"
        steps.append({
            "capability": ms.capability, "provider": ms.provider,
            "tier": next((s.tier for s in plan.connect_steps
                          if ms.provider == s.provider and ms.capability in s.capabilities), 0),
            "provenance": source_by.get(ms.capability, ""),
            "status": status,
            "reconciled": (sr.reconciled if sr else None),
        })
    refusals = [{"capability": r.dimension, "detail": r.detail} for r in plan.refusals]
    return Explanation(plan.intent_hash, plan.connectable, tuple(steps), tuple(refusals))
