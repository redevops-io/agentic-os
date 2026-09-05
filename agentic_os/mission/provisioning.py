"""Install planning — the Runtime deciding what the Runtime needs to install.

This is Sidekick's guided-configuration step, and it is the same capability-graph planning
the stack sells for applications, turned inward: an **outcome** ("help me review this GitHub
project", "connect my CRM and prepare follow-ups") resolves to the **capabilities** that
satisfy it, which resolve to the **modules** that must be installed, the **credentials** the
broker will need, the **storage** to configure, and the **cost** — all *inside the trust
envelope the device was granted* by :mod:`.device_posture`.

That last clause is the point of doing this after posture (P0.2): a capability whose
execution class this device DENYs cannot be installed for the outcome, and one at REVIEW is
flagged for a human. Sidekick never recruits beyond what the device is allowed to run.

No new capability model is invented — a *module* is a :class:`~.types.CapabilityManifest`
(what an app publishes at ``GET /capabilities``), and the planner reads the metadata already
on :class:`~.types.CapabilitySpec` (``provides``, ``secrets``, ``isolation_class``,
``network``, ``approval_required``, ``cost``). The matcher (outcome → capabilities) is
injectable, so today's keyword resolver can be swapped for an LLM one without touching the
gate or the plan shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Iterable, List, Tuple

from runtime_contracts.canonical import content_hash

from .device_posture import Decision, DevicePosture, ExecutionClass
from .types import CapabilityManifest, CapabilitySpec

CONTRACT_VERSION = "install-plan/v1"


class Topology(str, Enum):
    """How this install relates to others. A *capability preset*, not a fixed edition — a
    starting bundle Sidekick then widens as outcomes demand (see the program plan)."""

    STANDALONE = "standalone"   #: embedded state, local/external model, local operators
    MEMBER = "member"           #: joins a team: shared event store + model + federated peers
    HUB = "hub"                 #: hosts shared state / model / peer registry for members


class InstallBlocked(RuntimeError):
    """An outcome needs a capability this device is not allowed to run. Named, never degraded."""


# ── isolation_class (executor confinement) → posture ExecutionClass ──────────────────────
# Only an *explicit* contained confinement ("sandbox"/"strict") earns the easy LOCAL_CONTAINER
# tier; "in_process" and anything undeclared fall to HOST_PROCESS via the conservative default
# in execution_class_for — deny-by-default, so an author must declare containment to get it.
_ISOLATION_TO_CLASS: Dict[str, ExecutionClass] = {
    "sandbox": ExecutionClass.LOCAL_CONTAINER,
    "strict": ExecutionClass.LOCAL_CONTAINER,
    "in_process": ExecutionClass.HOST_PROCESS,
}


def execution_class_for(spec: CapabilitySpec) -> ExecutionClass:
    """The posture tier a capability must be granted to run. Unknown isolation ⇒ HOST_PROCESS
    (the more-restricted tier), so an undeclared confinement never silently gets the easy pass."""
    return _ISOLATION_TO_CLASS.get(spec.isolation_class, ExecutionClass.HOST_PROCESS)


def default_matcher(outcome: str, spec: CapabilitySpec) -> bool:
    """Keyword resolver: a capability matches when one of the outcomes it ``provides`` (or its
    name) is named in the request. Deterministic and offline; an LLM matcher drops in here."""
    o = outcome.lower()
    for token in list(spec.provides) + [spec.name]:
        if token and token.lower() in o:
            return True
    return False


@dataclass(frozen=True)
class ResolvedCapability:
    """One capability the outcome needs, with its posture decision on this device."""

    capability: str
    module: str
    execution_class: ExecutionClass
    decision: Decision
    secrets: Tuple[str, ...] = ()
    approval_required: bool = False
    cost_usd_milli: int = 0        # integer milli-dollars — floats have no canonical form

    def canonical_form(self) -> Dict[str, object]:
        return {
            "capability": self.capability, "module": self.module,
            "execution_class": self.execution_class.value, "decision": self.decision.value,
            "secrets": sorted(self.secrets), "approval_required": self.approval_required,
            "cost_usd_milli": self.cost_usd_milli,
        }


@dataclass(frozen=True)
class InstallPlan:
    """What must happen for this device to serve this outcome — content-addressed."""

    outcome: str
    topology: Topology
    resolved: Tuple[ResolvedCapability, ...]
    to_install: Tuple[str, ...] = ()
    already_installed: Tuple[str, ...] = ()
    credentials_needed: Tuple[str, ...] = ()
    storage_needed: bool = False
    needs_approval: Tuple[str, ...] = ()
    blocked: Tuple[Tuple[str, str], ...] = ()      # (capability, reason)
    est_cost_usd_milli: int = 0
    contract_version: str = CONTRACT_VERSION

    def canonical_form(self) -> Dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "outcome": self.outcome,
            "topology": self.topology.value,
            "resolved": [r.canonical_form() for r in sorted(
                self.resolved, key=lambda r: (r.module, r.capability))],
            "to_install": sorted(self.to_install),
            "already_installed": sorted(self.already_installed),
            "credentials_needed": sorted(self.credentials_needed),
            "storage_needed": self.storage_needed,
            "needs_approval": sorted(self.needs_approval),
            "blocked": sorted([list(b) for b in self.blocked]),
            "est_cost_usd_milli": self.est_cost_usd_milli,
        }

    @property
    def plan_id(self) -> str:
        return content_hash(self.canonical_form())

    @property
    def installable(self) -> bool:
        """True when nothing the outcome needs is DENY'd by the device posture."""
        return not self.blocked


def resolve(outcome: str, catalog: Iterable[CapabilityManifest], posture: DevicePosture, *,
            installed: Iterable[str] = (), topology: Topology = Topology.STANDALONE,
            matcher: Callable[[str, CapabilitySpec], bool] = default_matcher) -> InstallPlan:
    """Resolve an outcome to an :class:`InstallPlan`, gated by ``posture``.

    Each capability that satisfies the outcome is checked against the device posture: a DENY'd
    execution class becomes a ``blocked`` entry (the plan is then not :attr:`~InstallPlan.installable`);
    a REVIEW class or an ``approval_required`` capability is flagged in ``needs_approval``.
    Credentials (broker refs), storage need, cost, and installed-vs-to-install are aggregated.
    """
    installed_set = set(installed)
    resolved: List[ResolvedCapability] = []
    blocked: List[Tuple[str, str]] = []
    needs_approval: List[str] = []
    creds: set[str] = set()
    modules_needed: set[str] = set()
    storage_needed = False
    total_milli = 0

    for manifest in catalog:
        for spec in manifest.capabilities:
            if not matcher(outcome, spec):
                continue
            cls = execution_class_for(spec)
            decision = posture.classes.get(cls, Decision.DENY)
            cost_milli = int(round(spec.cost.usd * 1000))
            modules_needed.add(spec.operator)
            creds.update(spec.secrets)
            total_milli += cost_milli
            if spec.data_classifications:          # handles sensitive data ⇒ needs storage
                storage_needed = True
            resolved.append(ResolvedCapability(
                capability=spec.name, module=spec.operator, execution_class=cls,
                decision=decision, secrets=tuple(sorted(spec.secrets)),
                approval_required=spec.approval_required, cost_usd_milli=cost_milli))
            if decision is Decision.DENY:
                blocked.append((spec.name, f"{cls.value} denied by device posture"))
            elif decision is Decision.REVIEW or spec.approval_required:
                needs_approval.append(spec.name)

    return InstallPlan(
        outcome=outcome, topology=topology,
        resolved=tuple(resolved),
        to_install=tuple(sorted(modules_needed - installed_set)),
        already_installed=tuple(sorted(modules_needed & installed_set)),
        credentials_needed=tuple(sorted(creds)),
        storage_needed=storage_needed,
        needs_approval=tuple(sorted(set(needs_approval))),
        blocked=tuple(blocked),
        est_cost_usd_milli=total_milli,
    )


def require_installable(plan: InstallPlan) -> None:
    """Raise :class:`InstallBlocked` (with the blocking reasons) unless the plan is installable."""
    if not plan.installable:
        detail = "; ".join(f"{cap}: {reason}" for cap, reason in plan.blocked)
        raise InstallBlocked(detail or "outcome requires capabilities this device may not run")
