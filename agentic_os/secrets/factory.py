"""Secret-store selection + a production-assurance broker (P3).

One factory chooses the SecretStore backend by config/env so a deployment picks its store without touching
any capability code:

    dev / CI            → env               (EnvironmentSecretStore, development)
    local plaintext     → file              (FileSecretStore, development)
    single-user desktop → encrypted-file    (AES-GCM at rest, production-grade)
                        or keyring           (OS keychain: DPAPI / Keychain / Secret Service)
    server / enterprise → vault              (Vault / OpenBao KV v2, production-grade)

``build_credential_broker`` pairs a production-grade store with a ProductionCredentialBroker (so
``production_broker_required`` capabilities may run) and the plain dev stores with the development
LocalCredentialBroker. The broker interface the capability uses is identical either way.
"""
from __future__ import annotations

import os
from typing import Any, Tuple

from runtime_contracts.secrets_local.broker import LocalCredentialBroker
from runtime_contracts.secrets_local.store import EnvironmentSecretStore, FileSecretStore

from .stores import EncryptedFileSecretStore, KeyringSecretStore, VaultSecretStore


class ProductionCredentialBroker(LocalCredentialBroker):
    """A credential broker that asserts **production** assurance, so capabilities declaring
    ``production_broker_required=True`` (fail-closed against a development broker) may run.

    It reuses LocalCredentialBroker's authority-scoping, TTL-bounding, Mission/capability redemption
    binding and revoke logic, over a production-grade SecretStore (Vault/OpenBao, or an encrypted local /
    OS-keychain store). It deliberately does NOT claim enterprise distributed leases, dynamic database
    users or HA revocation — it asserts a real, non-plaintext secret backend plus authority-scoped
    brokerage, which is the boundary this tier is responsible for."""

    assurance_level = "production"


_STORES = {
    "env": lambda **c: EnvironmentSecretStore(),
    "file": lambda **c: FileSecretStore(c.get("root")),
    "encrypted-file": lambda **c: EncryptedFileSecretStore(c.get("root"), key=c.get("key")),
    # `keyring_backend` (not `backend`) so the injected keychain never collides with the factory's own
    # `backend` selector argument.
    "keyring": lambda **c: KeyringSecretStore(
        service_prefix=c.get("service_prefix", "redevops"), backend=c.get("keyring_backend")),
    "vault": lambda **c: VaultSecretStore(
        addr=c.get("addr"), token=c.get("token"), mount=c.get("mount", "secret"),
        transport=c.get("transport")),
}


def build_secret_store(backend: str | None = None, **cfg: Any):
    """Construct the SecretStore for ``backend`` (or ``REDEVOPS_SECRET_BACKEND``, default ``env``)."""
    backend = (backend or os.environ.get("REDEVOPS_SECRET_BACKEND") or "env").lower()
    make = _STORES.get(backend)
    if make is None:
        raise ValueError(f"unknown secret backend '{backend}' (one of {sorted(_STORES)})")
    return make(**cfg)


def build_credential_broker(backend: str | None = None, **cfg: Any) -> Tuple[Any, LocalCredentialBroker]:
    """Return ``(store, broker)`` for ``backend``. A production-grade store gets a ProductionCredentialBroker;
    the plain env/file dev stores get the development LocalCredentialBroker. Capability code holds the broker
    and never learns which store is underneath."""
    store = build_secret_store(backend, **cfg)
    production = getattr(store, "is_production_grade", False)
    broker = ProductionCredentialBroker(store) if production else LocalCredentialBroker(store)
    return store, broker
