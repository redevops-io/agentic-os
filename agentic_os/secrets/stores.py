"""Production-grade SecretStore backends behind the runtime's CredentialBroker seam (P3).

The runtime already ships development-grade stores (``EnvironmentSecretStore`` for dev/CI, ``FileSecretStore``
for local plaintext) plus the authority-scoped ``LocalCredentialBroker``. This module adds three real
backends — all structurally conformant to the ``runtime_contracts`` ``SecretStore`` protocol
(``describe`` / ``put`` / ``rotate`` / ``revoke`` / ``_read``), so the broker and every capability that
resolves a credential work against them UNCHANGED:

  * :class:`EncryptedFileSecretStore` — AES-GCM at rest for a single-user local/desktop install.
  * :class:`KeyringSecretStore`       — OS-native secret service (DPAPI / macOS Keychain / Secret Service).
  * :class:`VaultSecretStore`         — HashiCorp Vault / OpenBao KV v2 for server / multi-user / enterprise.

None of these ever hands the secret to arbitrary runtime code: the value is reached only through ``_read``,
which the broker calls at the capability boundary. Nothing here logs a path together with its value.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from runtime_contracts.protocol.secrets import SecretDescriptor, SecretRef
from runtime_contracts.secrets_local.store import SecretAccessError


# ── local encrypted file store (single-user desktop) ───────────────────────────────────────────────────

class EncryptedFileSecretStore:
    """File-backed store with **AES-GCM encryption at rest**. The master key is injected (or read from
    ``REDEVOPS_SECRET_MASTER_KEY`` as base64) — in a packaged desktop it comes from the OS keychain, so the
    files on disk are ciphertext and the key never sits beside them. Hardened like ``FileSecretStore``:
    confined to a canonical root, refuses symlinks and world/group-accessible files, 0600 on write."""

    provider = "encrypted-file"
    is_production_grade = True

    def __init__(self, root: Optional[str] = None, *, key: Optional[bytes] = None) -> None:
        root = root or os.environ.get("REDEVOPS_SECRET_DIR", "")
        if not root:
            raise SecretAccessError("EncryptedFileSecretStore needs a root (REDEVOPS_SECRET_DIR)")
        self.root = os.path.realpath(root)
        self._key = key if key is not None else self._key_from_env()
        if len(self._key) not in (16, 24, 32):
            raise SecretAccessError("master key must be 16/24/32 bytes (AES-128/192/256)")

    @staticmethod
    def _key_from_env() -> bytes:
        raw = os.environ.get("REDEVOPS_SECRET_MASTER_KEY", "")
        if not raw:
            raise SecretAccessError("no master key: set REDEVOPS_SECRET_MASTER_KEY (base64) or pass key=")
        try:
            return base64.b64decode(raw)
        except Exception as e:  # noqa: BLE001
            raise SecretAccessError("REDEVOPS_SECRET_MASTER_KEY is not valid base64") from e

    def _resolve(self, namespace: str, path: str) -> str:
        target = os.path.realpath(os.path.join(self.root, namespace, path + ".enc"))
        if target != self.root and not target.startswith(self.root + os.sep):
            raise SecretAccessError("secret path escapes the store root")
        return target

    def _check_safe(self, target: str) -> None:
        if not os.path.exists(target):
            raise SecretAccessError("secret not found")
        if os.path.islink(target):
            raise SecretAccessError("refusing to read a symlinked secret")
        if os.lstat(target).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SecretAccessError("refusing world/group-accessible secret file")

    def _seal(self, plaintext: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        return nonce + AESGCM(self._key).encrypt(nonce, plaintext, None)

    def _open(self, blob: bytes) -> bytes:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            return AESGCM(self._key).decrypt(blob[:12], blob[12:], None)
        except (InvalidTag, ValueError) as e:
            raise SecretAccessError("secret could not be decrypted (wrong master key or corrupt file)") from e

    def describe(self, ref: SecretRef) -> SecretDescriptor:
        self._check_safe(self._resolve(ref.namespace, ref.path))
        return SecretDescriptor(ref=ref, classifications=("credential",), rotatable=True)

    def _read(self, ref: SecretRef) -> bytes:
        target = self._resolve(ref.namespace, ref.path)
        self._check_safe(target)
        with open(target, "rb") as fh:
            return self._open(fh.read())

    def put(self, *, namespace: str, path: str, value: bytes,
            classifications: Tuple[str, ...] = (), metadata: Optional[Dict[str, str]] = None) -> SecretRef:
        target = self._resolve(namespace, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, self._seal(value))
        finally:
            os.close(fd)
        os.chmod(target, 0o600)
        return SecretRef(provider=self.provider, namespace=namespace, path=path, version="1")

    def rotate(self, ref: SecretRef) -> SecretRef:
        # Re-seal in place with a fresh nonce (new ciphertext at rest) and bump the version label.
        value = self._read(ref)
        self.put(namespace=ref.namespace, path=ref.path, value=value)
        nxt = str(int(ref.version or "1") + 1)
        return SecretRef(provider=self.provider, namespace=ref.namespace, path=ref.path, key=ref.key, version=nxt)

    def revoke(self, ref: SecretRef) -> None:
        target = self._resolve(ref.namespace, ref.path)
        if os.path.exists(target) and not os.path.islink(target):
            os.remove(target)


# ── OS-native keychain store (single-user desktop) ─────────────────────────────────────────────────────

class KeyringSecretStore:
    """Store backed by the OS secret service via the ``keyring`` library — Windows Credential Manager/DPAPI,
    macOS Keychain, or the Linux Secret Service. The backend is injected (defaults to the ``keyring`` module),
    so it is testable with a fake and never hard-depends on a live keychain. Values are stored base64 so
    arbitrary bytes round-trip. ``namespace`` becomes the keychain *service*, ``path`` the *account*."""

    provider = "keyring"
    is_production_grade = True

    def __init__(self, *, service_prefix: str = "redevops", backend: Any = None) -> None:
        self._prefix = service_prefix
        if backend is None:
            import keyring  # noqa: PLC0415 — optional; only needed for the real OS keychain
            backend = keyring
        self._kr = backend

    def _service(self, namespace: str) -> str:
        return f"{self._prefix}:{namespace}" if namespace else self._prefix

    def describe(self, ref: SecretRef) -> SecretDescriptor:
        if self._kr.get_password(self._service(ref.namespace), ref.path) is None:
            raise SecretAccessError(f"keyring secret not found: {ref.redacted()}")
        return SecretDescriptor(ref=ref, classifications=("credential",), rotatable=True)

    def _read(self, ref: SecretRef) -> bytes:
        val = self._kr.get_password(self._service(ref.namespace), ref.path)
        if val is None:
            raise SecretAccessError(f"keyring secret not found: {ref.redacted()}")
        return base64.b64decode(val)

    def put(self, *, namespace: str, path: str, value: bytes,
            classifications: Tuple[str, ...] = (), metadata: Optional[Dict[str, str]] = None) -> SecretRef:
        self._kr.set_password(self._service(namespace), path, base64.b64encode(value).decode())
        return SecretRef(provider=self.provider, namespace=namespace, path=path, version="1")

    def rotate(self, ref: SecretRef) -> SecretRef:
        # The OS keychain has no native versioning; the value is unchanged, only the label advances.
        nxt = str(int(ref.version or "1") + 1)
        return SecretRef(provider=self.provider, namespace=ref.namespace, path=ref.path, key=ref.key, version=nxt)

    def revoke(self, ref: SecretRef) -> None:
        try:
            self._kr.delete_password(self._service(ref.namespace), ref.path)
        except Exception:  # noqa: BLE001 — deleting a missing entry is a no-op
            pass


# ── Vault / OpenBao KV v2 store (server / multi-user / enterprise) ─────────────────────────────────────

def _urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Dict[str, Any]:
    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310
        raw = r.read().decode()
    return json.loads(raw) if raw else {}


class VaultSecretStore:
    """HashiCorp Vault / OpenBao **KV v2** store. Reads/writes ``{mount}/data/{namespace}/{path}`` with a
    token, over an injectable ``transport`` (default: urllib) so it is testable without a live Vault. A
    SecretRef's ``key`` names the field within the KV item (default ``value``). This is the deployment
    backend for server / multi-user / enterprise installs; the desktop uses the encrypted-file or keyring
    store instead — capability code never knows which."""

    provider = "vault"
    is_production_grade = True

    def __init__(self, *, addr: Optional[str] = None, token: Optional[str] = None, mount: str = "secret",
                 transport: Optional[Callable[..., Dict[str, Any]]] = None) -> None:
        self.addr = (addr or os.environ.get("VAULT_ADDR", "")).rstrip("/")
        self.token = token or os.environ.get("VAULT_TOKEN", "")
        self.mount = mount
        self._t = transport or _urllib_transport
        if not self.addr:
            raise SecretAccessError("VaultSecretStore needs an address (VAULT_ADDR)")

    def _headers(self) -> Dict[str, str]:
        return {"X-Vault-Token": self.token, "Content-Type": "application/json"}

    def _data_url(self, namespace: str, path: str) -> str:
        return f"{self.addr}/v1/{self.mount}/data/{namespace}/{path}".replace("//v1", "/v1")

    def _field(self, ref: SecretRef) -> str:
        return ref.key or "value"

    def describe(self, ref: SecretRef) -> SecretDescriptor:
        self._read(ref)  # raises if absent
        return SecretDescriptor(ref=ref, classifications=("credential",), rotatable=True, renewable=True)

    def _read(self, ref: SecretRef) -> bytes:
        try:
            resp = self._t("GET", self._data_url(ref.namespace, ref.path), self._headers(), None)
        except Exception as e:  # noqa: BLE001
            raise SecretAccessError(f"vault read failed: {type(e).__name__}") from None
        data = ((resp.get("data") or {}).get("data")) or {}
        field = self._field(ref)
        if field not in data:
            raise SecretAccessError(f"vault secret has no field '{field}': {ref.redacted()}")
        return str(data[field]).encode()

    def put(self, *, namespace: str, path: str, value: bytes,
            classifications: Tuple[str, ...] = (), metadata: Optional[Dict[str, str]] = None) -> SecretRef:
        field = "value"
        body = json.dumps({"data": {field: value.decode("utf-8", "replace")}}).encode()
        resp = self._t("POST", self._data_url(namespace, path), self._headers(), body)
        version = str(((resp.get("data") or {}).get("version")) or "1")
        return SecretRef(provider=self.provider, namespace=namespace, path=path, key=field, version=version)

    def rotate(self, ref: SecretRef) -> SecretRef:
        # KV v2 versions on write: re-put the current value to mint a new version (a real rotation swaps in
        # a fresh dynamic secret; this preserves the value while advancing the version).
        return self.put(namespace=ref.namespace, path=ref.path, value=self._read(ref))

    def revoke(self, ref: SecretRef) -> None:
        url = f"{self.addr}/v1/{self.mount}/metadata/{ref.namespace}/{ref.path}".replace("//v1", "/v1")
        try:
            self._t("DELETE", url, self._headers(), None)
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
