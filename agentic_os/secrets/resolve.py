"""Resolve a secret REFERENCE ("backend:namespace/path") through the credential broker, on every use.

A reference names where a secret lives (``file:llm/provider``, ``vault:llm/provider``, ``keyring:llm/provider``,
``keepassxc:Redevops/LLM``, ``env:OPENAI_API_KEY``); it never contains the value. ``read_secret`` performs the same
grant -> redeem -> destroy -> revoke ceremony the apps use for their other credentials, so the value exists only for
the call, and a change in the store (a rotation) is picked up on the very next call with no restart.
``store_secret`` writes a value into the store a reference points at (used by the Sidekick settings skill)."""
from __future__ import annotations

import uuid
from typing import Any, Optional, Tuple

from runtime_contracts.protocol.secrets import CredentialRequest, CredentialRequirement, SecretRef
from runtime_contracts.protocol.security import AuthorityContext, PrincipalRef
from runtime_contracts.secrets_local.broker import CredentialDenied

from .factory import build_credential_broker, build_secret_store


class SecretReferenceError(ValueError):
    pass


def parse_ref(ref: str) -> Tuple[str, str, str]:
    """``backend:namespace/path`` -> (backend, namespace, path). ``env:NAME`` -> ("env", "", "NAME")."""
    backend, sep, rest = (ref or "").strip().partition(":")
    if not sep or not backend or not rest or any(c in ref for c in "\n\r\x00"):
        raise SecretReferenceError("a secret reference looks like 'backend:namespace/path'")
    if backend == "env":
        return "env", "", rest
    ns, slash, path = rest.partition("/")
    if not slash or not ns or not path or ".." in rest.split("/"):
        raise SecretReferenceError("a secret reference looks like 'backend:namespace/path'")
    return backend, ns, path


def _secret_ref(backend: str, ns: str, path: str) -> SecretRef:
    if backend == "env":
        return SecretRef(provider="env", key=path)
    return SecretRef(provider=backend, namespace=ns, path=path, key="value" if backend == "vault" else None)


def read_secret(ref: str, *, purpose: str = "llm-provider", capability: str = "llm.invoke",
                tenant: str = "default", mission: str = "llm", **store_cfg: Any) -> Optional[str]:
    """The secret's value for this one call, or ``None`` when the broker denies or the secret is absent (the caller
    then degrades; it never falls back to a different credential silently)."""
    backend, ns, path = parse_ref(ref)
    _, broker = build_credential_broker(backend, **store_cfg)
    authority = AuthorityContext(authority_id=mission, principal=PrincipalRef(id=mission, kind="service", tenant=tenant),
                                 purpose=purpose, scope=(capability,))
    requirement = CredentialRequirement(name=purpose, required_scopes=(capability,),
                                        secret_ref=_secret_ref(backend, ns, path), max_ttl_seconds=60)
    request = CredentialRequest(request_id=uuid.uuid4().hex, mission_id=mission, node_id="llm", capability_id=capability,
                                tenant_id=tenant, authority_context_id=authority.chain_ref, requirement=requirement,
                                requested_ttl_seconds=60)
    grant = None
    try:
        grant = broker.grant(request, authority_context=authority)
        material = broker.redeem(grant, capability_id=capability, mission_id=mission)
    except CredentialDenied:
        return None
    except Exception:  # noqa: BLE001 - SecretAccessError (absent / locked database) -> degrade
        if grant is not None:
            try:
                broker.revoke_grant(grant.grant_id, reason="resolve-failed")
            except Exception:  # noqa: BLE001
                pass
        return None
    try:
        return material.bytes().decode("utf-8", "replace")
    finally:
        material.destroy()
        try:
            broker.revoke_grant(grant.grant_id, reason="after use")
        except Exception:  # noqa: BLE001
            pass


def store_secret(ref: str, value: str, **store_cfg: Any) -> SecretRef:
    """Write ``value`` where ``ref`` points (create or replace). Returns the reference (never the value). The
    env backend is read-only: persist the key in a real store or keep using your exported variable."""
    backend, ns, path = parse_ref(ref)
    if backend == "env":
        raise SecretReferenceError("an env reference is read-only: export the variable yourself, or choose a store "
                                   "(file, encrypted-file, keyring, vault, keepassxc) to persist the key")
    if not value or "\x00" in value:
        raise SecretReferenceError("empty or invalid secret value")
    store = build_secret_store(backend, **store_cfg)
    return store.put(namespace=ns, path=path, value=value.encode("utf-8"))


__all__ = ["SecretReferenceError", "parse_ref", "read_secret", "store_secret"]
