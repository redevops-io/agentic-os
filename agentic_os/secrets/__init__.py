"""Production SecretStore backends + broker selection (P3 of the unified-desktop redesign).

Concrete, non-plaintext stores behind the runtime's authority-scoped CredentialBroker seam — an encrypted
local file store and an OS-keychain store for single-user desktop installs, and a Vault/OpenBao store for
server/enterprise — plus a factory that selects one by config so capability code stays store-agnostic.
"""
from .factory import (
    ProductionCredentialBroker,
    build_credential_broker,
    build_secret_store,
)
from .keepassxc import KeePassXCSecretStore
from .resolve import read_secret, store_secret
from .stores import EncryptedFileSecretStore, KeyringSecretStore, VaultSecretStore

__all__ = [
    "KeePassXCSecretStore",
    "read_secret",
    "store_secret",
    "EncryptedFileSecretStore",
    "KeyringSecretStore",
    "VaultSecretStore",
    "ProductionCredentialBroker",
    "build_secret_store",
    "build_credential_broker",
]
