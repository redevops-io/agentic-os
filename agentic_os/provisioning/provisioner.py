"""ServiceProvisioner — per-service install / initialize / health / uninstall (P0 contract).

The open runtime had only whole-stack lifecycle (start the control plane; ``docker compose up`` the bundle).
This adds the per-service lifecycle Bootstrap needs: deploy a service, create its service account +
credential (via :class:`ServiceIdentityBroker`), register its endpoint + capabilities, health-check it —
and, on removal, **revoke the identity** so no orphaned service account is left behind.

``uninstall`` calling ``revoke`` is a **contract obligation**, not a nicety: orphaned credentials after an
app is removed are the most common leak in this class of system.

How a service is actually run is pluggable (:class:`ServiceRunner`) so this works headlessly today and
targets k8s / a sidecar / a separate machine later — the identity + credential + registration flow is the
same regardless of the runner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from .identity import ServiceIdentity, ServiceIdentityBroker


class ServiceState:
    PENDING = "pending"
    INSTALLED = "installed"
    INITIALIZED = "initialized"
    HEALTHY = "healthy"
    FAILED = "failed"
    UNINSTALLED = "uninstalled"


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    domain: str = ""
    capabilities: Tuple[str, ...] = ()
    tenant: str = ""
    roles: Tuple[str, ...] = ()
    identity_supports: Tuple[str, ...] = ()   # rungs the target offers ((): internal → service token)
    health_path: str = "/health"


@dataclass
class ServiceRecord:
    name: str
    state: str
    endpoint: str = ""
    identity: Optional[ServiceIdentity] = None
    capabilities: Tuple[str, ...] = ()
    healthy: bool = False

    def as_dict(self) -> Dict[str, Any]:
        # registry view: endpoints, capabilities, identity rung + credential *ref*, health — no secret.
        return {"name": self.name, "state": self.state, "endpoint": self.endpoint,
                "capabilities": list(self.capabilities), "healthy": self.healthy,
                "identity": self.identity.as_dict() if self.identity else None}


class ServiceRunner(Protocol):
    def install(self, spec: ServiceSpec) -> str: ...          # returns the service endpoint
    def start(self, name: str) -> None: ...
    def stop(self, name: str) -> None: ...
    def health(self, name: str, endpoint: str) -> bool: ...
    def uninstall(self, name: str) -> None: ...


class RegistryRunner:
    """The default in-process runner: no real process — it registers an endpoint and answers health from an
    injectable ``health_fn`` (default: a started service is healthy). Enough to run + test the whole
    provisioning flow headlessly; a KubernetesRunner / DockerRunner / RemoteRunner drops in later."""

    def __init__(self, *, base: str = "http://127.0.0.1:8800",
                 health_fn: Optional[Callable[[str, str], bool]] = None) -> None:
        self._base = base.rstrip("/")
        self._health_fn = health_fn
        self._started: set = set()

    def install(self, spec: ServiceSpec) -> str:
        return f"{self._base}/{spec.name}"

    def start(self, name: str) -> None:
        self._started.add(name)

    def stop(self, name: str) -> None:
        self._started.discard(name)

    def health(self, name: str, endpoint: str) -> bool:
        if self._health_fn is not None:
            return bool(self._health_fn(name, endpoint))
        return name in self._started

    def uninstall(self, name: str) -> None:
        self._started.discard(name)


class ProvisionError(RuntimeError):
    pass


class ServiceProvisioner:
    def __init__(self, *, identity_broker: Optional[ServiceIdentityBroker] = None,
                 runner: Optional[ServiceRunner] = None,
                 records: Optional[Dict[str, ServiceRecord]] = None) -> None:
        self._id = identity_broker or ServiceIdentityBroker()
        self._runner = runner or RegistryRunner()
        self._records: Dict[str, ServiceRecord] = records if records is not None else {}

    # ── lifecycle ────────────────────────────────────────────────────────────
    def install(self, spec: ServiceSpec) -> ServiceRecord:
        """Create the service's identity + credential, deploy it, and register its endpoint + capabilities."""
        identity = self._id.create_identity(spec.name, tenant=spec.tenant, roles=spec.roles,
                                             supports=spec.identity_supports)
        endpoint = self._runner.install(spec)
        rec = ServiceRecord(name=spec.name, state=ServiceState.INSTALLED, endpoint=endpoint,
                            identity=identity, capabilities=spec.capabilities, healthy=False)
        self._records[spec.name] = rec
        return rec

    def initialize(self, name: str) -> ServiceRecord:
        rec = self._require(name)
        self._runner.start(name)
        rec.state = ServiceState.INITIALIZED
        return rec

    def health(self, name: str) -> bool:
        rec = self._require(name)
        ok = self._runner.health(name, rec.endpoint)
        rec.healthy = ok
        if rec.state in (ServiceState.INITIALIZED, ServiceState.HEALTHY, ServiceState.FAILED):
            rec.state = ServiceState.HEALTHY if ok else ServiceState.FAILED
        return ok

    def uninstall(self, name: str) -> None:
        """Tear the service down AND revoke its identity/credential — contract obligation (no orphans)."""
        rec = self._records.get(name)
        self._runner.stop(name)
        self._runner.uninstall(name)
        self._id.revoke(name)                      # <-- the obligation
        if rec is not None:
            rec.state = ServiceState.UNINSTALLED
            rec.healthy = False

    # ── views ────────────────────────────────────────────────────────────────
    def registry(self) -> List[Dict[str, Any]]:
        """The credential-invisible view of what's provisioned (for capability discovery + the UI)."""
        return [rec.as_dict() for rec in self._records.values()]

    def secret_ref(self, name: str):
        """Where the Runtime resolves this service's own credential (a reference, never the value)."""
        rec = self._records.get(name)
        return rec.identity.secret_ref if rec and rec.identity else None

    def _require(self, name: str) -> ServiceRecord:
        rec = self._records.get(name)
        if rec is None:
            raise ProvisionError(f"service '{name}' is not installed")
        return rec


def bootstrap(specs: List[ServiceSpec], *, provisioner: Optional[ServiceProvisioner] = None
              ) -> ServiceProvisioner:
    """The install-flow middle band: deploy each service → create its service account + credential → store
    in the secrets plane → register capabilities + endpoints → health-check. Returns the provisioner whose
    ``registry()`` is the capability-discovery surface the unified UI then reads."""
    prov = provisioner or ServiceProvisioner()
    for spec in specs:
        prov.install(spec)
        prov.initialize(spec.name)
        prov.health(spec.name)
    return prov
