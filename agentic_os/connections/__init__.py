"""Central Connections surface (P0 contract) — key-first today, class-aware, credential-invisible.

Users interact with connections and permissions; the runtime handles credentials. Every provider is
classified agent-native / oauth-native / ui-bound; connecting deposits the credential into the writable
SecretStore (the P3 broker seam) and records only connection state. See REDEVOPS_API_FIRST_INTEGRATIONS_MATRIX.md.
"""
from .manager import ConnectionError, ConnectionManager, ConnectionRefused
from .registry import default_registry
from .types import ConnectionState, IntegrationClass, ProviderSpec, VerifyResult

__all__ = [
    "ConnectionManager",
    "ConnectionError",
    "ConnectionRefused",
    "ConnectionState",
    "ProviderSpec",
    "VerifyResult",
    "IntegrationClass",
    "default_registry",
]
