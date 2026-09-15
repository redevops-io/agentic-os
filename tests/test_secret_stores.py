"""P3 — production SecretStore backends behind the CredentialBroker seam.

Proves: each backend round-trips a credential THROUGH the broker (so capability code is unchanged), the
encrypted store is ciphertext at rest and fails closed on the wrong key, the factory selects by config, the
broker/executor path is store-agnostic, and a production-grade store lifts the fail-closed gate that a
development store enforces.
"""
from __future__ import annotations

import base64
import json
import os

import pytest

from agentic_os.mission.executor import Executor, InMemoryOperatorClient, OperatorError
from agentic_os.mission.types import Node
from agentic_os.secrets import (
    EncryptedFileSecretStore, KeyringSecretStore, VaultSecretStore,
    ProductionCredentialBroker, build_credential_broker, build_secret_store)
from runtime_contracts import (
    AuthorityContext, CredentialRequirement, LocalCredentialBroker, PrincipalRef, SecretRef)
from runtime_contracts.secrets_local.store import SecretAccessError

KEY = base64.b64decode(base64.b64encode(bytes(range(32))))  # a deterministic 32-byte AES-256 key


def _authority(scopes=("repo:deploy",)):
    return AuthorityContext(authority_id="ctx", principal=PrincipalRef(id="p", tenant="acme"),
                            purpose="use", scope=tuple(scopes))


def _resolve_through_broker(broker, ref, *, production_required=False, authority=None):
    """Run a credentialed node through the Executor+broker and return the value the handler received —
    the exact path a real capability uses; proves the store is reached only via the broker."""
    seen = {}

    def handler(inputs, secrets):
        seen["v"] = secrets["cred"].bytes()
        return {"ok": True}

    def creds_for(node):
        return (CredentialRequirement(name="cred", required_scopes=("repo:deploy",), secret_ref=ref,
                                      production_broker_required=production_required, max_ttl_seconds=300),)
    ex = Executor(InMemoryOperatorClient({"do": handler}),
                  authority=authority or _authority(), broker=broker, credentials_for=creds_for)
    ex.run(Node(capability="do", operator="op"), {})
    return seen.get("v")


# ── encrypted-file ─────────────────────────────────────────────────────────────────────────────────────

def test_encrypted_file_roundtrip_through_broker_and_ciphertext_at_rest(tmp_path):
    store = EncryptedFileSecretStore(str(tmp_path), key=KEY)
    ref = store.put(namespace="creds", path="api", value=b"super-secret-value")
    assert ref.provider == "encrypted-file"

    # at rest the file is ciphertext — the plaintext never appears on disk
    raw = (tmp_path / "creds" / "api.enc").read_bytes()
    assert b"super-secret-value" not in raw and len(raw) > 12

    broker = ProductionCredentialBroker(store)
    assert _resolve_through_broker(broker, ref) == b"super-secret-value"


def test_encrypted_file_wrong_key_fails_closed(tmp_path):
    EncryptedFileSecretStore(str(tmp_path), key=KEY).put(namespace="c", path="k", value=b"v")
    other = EncryptedFileSecretStore(str(tmp_path), key=bytes(range(1, 33)))
    with pytest.raises(SecretAccessError):
        other._read(SecretRef(provider="encrypted-file", namespace="c", path="k"))


def test_encrypted_file_rotate_and_revoke(tmp_path):
    store = EncryptedFileSecretStore(str(tmp_path), key=KEY)
    ref = store.put(namespace="c", path="k", value=b"v1")
    raw1 = (tmp_path / "c" / "k.enc").read_bytes()
    ref2 = store.rotate(ref)
    assert ref2.version == "2"
    assert (tmp_path / "c" / "k.enc").read_bytes() != raw1     # fresh nonce → new ciphertext
    assert store._read(ref2) == b"v1"                          # same plaintext
    store.revoke(ref2)
    with pytest.raises(SecretAccessError):
        store._read(ref2)


# ── keyring (OS keychain) with an injected fake backend ─────────────────────────────────────────────────

class _FakeKeyring:
    def __init__(self):
        self._d = {}

    def get_password(self, service, user):
        return self._d.get((service, user))

    def set_password(self, service, user, val):
        self._d[(service, user)] = val

    def delete_password(self, service, user):
        self._d.pop((service, user), None)


def test_keyring_roundtrip_through_broker():
    store = KeyringSecretStore(backend=_FakeKeyring())
    ref = store.put(namespace="crm", path="twenty", value=b"kc-secret")
    assert ref.provider == "keyring"
    assert _resolve_through_broker(ProductionCredentialBroker(store), ref) == b"kc-secret"
    store.revoke(ref)
    with pytest.raises(SecretAccessError):
        store._read(ref)


# ── vault / openbao KV v2 with an injected fake transport ───────────────────────────────────────────────

def _fake_vault_transport():
    db = {}

    def t(method, url, headers, body):
        assert headers.get("X-Vault-Token") is not None
        if "/data/" in url:
            k = url.split("/data/", 1)[1]
            if method == "POST":
                data = json.loads(body)["data"]
                db[k] = {"data": data, "version": db.get(k, {}).get("version", 0) + 1}
                return {"data": {"version": db[k]["version"]}}
            if method == "GET":
                if k not in db:
                    raise RuntimeError("404")
                return {"data": {"data": db[k]["data"]}}
        if "/metadata/" in url and method == "DELETE":
            db.pop(url.split("/metadata/", 1)[1], None)
            return {}
        return {}
    return t


def test_vault_roundtrip_through_broker():
    store = VaultSecretStore(addr="http://vault:8200", token="t", transport=_fake_vault_transport())
    ref = store.put(namespace="crm", path="twenty", value=b"vault-secret")
    assert ref.provider == "vault" and ref.key == "value" and ref.version == "1"
    assert _resolve_through_broker(ProductionCredentialBroker(store), ref) == b"vault-secret"
    assert store.rotate(ref).version == "2"


# ── factory + store-agnostic broker + fail-closed gate ──────────────────────────────────────────────────

def test_factory_selects_backend(tmp_path):
    assert build_secret_store("encrypted-file", root=str(tmp_path), key=KEY).provider == "encrypted-file"
    assert build_secret_store("vault", addr="http://v", token="t",
                              transport=_fake_vault_transport()).provider == "vault"
    with pytest.raises(ValueError):
        build_secret_store("nope")


def test_broker_is_store_agnostic(tmp_path, monkeypatch):
    """The SAME capability path yields the SAME value across env / encrypted-file / keyring / vault —
    capability code never learns which store is underneath."""
    monkeypatch.setenv("GH_TOKEN", "agnostic")
    cases = [
        ("env", {}, SecretRef(provider="env", key="GH_TOKEN")),
        ("encrypted-file", {"root": str(tmp_path), "key": KEY}, None),
        ("keyring", {"keyring_backend": _FakeKeyring()}, None),
        ("vault", {"addr": "http://v", "token": "t", "transport": _fake_vault_transport()}, None),
    ]
    for backend, cfg, env_ref in cases:
        store, broker = build_credential_broker(backend, **cfg)
        ref = env_ref if env_ref is not None else store.put(namespace="ns", path="gh", value=b"agnostic")
        assert _resolve_through_broker(broker, ref) == b"agnostic", backend


def test_production_gate_dev_denies_production_grade_allows(tmp_path, monkeypatch):
    """A capability that declares production_broker_required=True fails closed on the development (env)
    broker and runs on a production-grade (encrypted/vault) broker."""
    monkeypatch.setenv("GH_TOKEN", "x")
    dev_store, dev_broker = build_credential_broker("env")
    assert isinstance(dev_broker, LocalCredentialBroker) and dev_broker.assurance_level == "development"
    with pytest.raises(OperatorError):        # fail closed
        _resolve_through_broker(dev_broker, SecretRef(provider="env", key="GH_TOKEN"),
                                production_required=True)

    prod_store, prod_broker = build_credential_broker("encrypted-file", root=str(tmp_path), key=KEY)
    assert prod_broker.assurance_level == "production"
    ref = prod_store.put(namespace="ns", path="gh", value=b"prod-ok")
    assert _resolve_through_broker(prod_broker, ref, production_required=True) == b"prod-ok"
