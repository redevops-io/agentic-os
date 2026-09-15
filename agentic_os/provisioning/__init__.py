"""Central Identity & Provisioning (P0) — the machine-identity + per-service lifecycle half of Bootstrap.

ServiceIdentityBroker mints/rotates/revokes a service account + its credential (identity-preference ladder,
credential stored via the P3 broker seam); ServiceProvisioner installs/initializes/health-checks/uninstalls
a service and registers its endpoint + capabilities — with uninstall revoking the identity by contract.
Together they realise the install flow's middle band: deploy → create service account → generate credential
→ store in the secrets plane → register capabilities + endpoints → health/capability discovery.
"""
from .identity import (
    IdentityError,
    IdentityRung,
    ServiceIdentity,
    ServiceIdentityBroker,
    pick_rung,
)
from .provisioner import (
    ProvisionError,
    RegistryRunner,
    ServiceProvisioner,
    ServiceRecord,
    ServiceRunner,
    ServiceSpec,
    ServiceState,
    bootstrap,
)

__all__ = [
    "IdentityRung",
    "ServiceIdentity",
    "ServiceIdentityBroker",
    "IdentityError",
    "pick_rung",
    "ServiceProvisioner",
    "ServiceSpec",
    "ServiceRecord",
    "ServiceState",
    "ServiceRunner",
    "RegistryRunner",
    "ProvisionError",
    "bootstrap",
]
