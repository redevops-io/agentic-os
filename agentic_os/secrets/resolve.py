"""Resolve a secret REFERENCE ("backend:namespace/path") through the credential broker, on every use.

A reference names where a secret lives (``file:llm/provider``, ``vault:llm/provider``, ``keyring:llm/provider``,
``keepassxc:Redevops/LLM``, ``env:OPENAI_API_KEY``); it never contains the value. ``read_secret`` performs the same
grant -> redeem -> destroy -> revoke ceremony the apps use for their other credentials, so the value exists only for
the call, and a change in the store (a rotation) is picked up on the very next call with no restart.
``store_secret`` writes a value into the store a reference points at (used by the Sidekick settings skill).

TENANT. runtime-contracts >= 0.3.15 refuses a credential request unless the request tenant equals the tenant of the
authority's principal AND the secret reference's namespace equals that tenant (``TENANT_MISMATCH``), the authority has
not expired (``AUTHORITY_EXPIRED``) and the request names the authority (``AUTHORITY_MISMATCH``). ``read_secret`` issues
its own one-call service authority, so the tenant it puts in that authority decides what the gate can protect. The
tenant comes from exactly three places, in this order, and from nowhere else:

1. the ``tenant`` argument, when the caller names one (an embedding caller names it; nothing in this edition
   authenticates a principal for ``read_secret``). It is used as given, never repaired: an empty or malformed tenant is
   refused, and a tenant that differs from the reference's namespace is refused by the contract (``TENANT_MISMATCH``);
2. the local tenant the deployment configures in ``REDEVOPS_LOCAL_TENANT``. Every namespaced reference must then live in
   that namespace (``env:`` references, which have none, are read as that tenant). It pins one local tenant; it is not
   tenant isolation (docs/EDITION_BOUNDARY.md, B1);
3. otherwise the single-user desktop rule, stated here and not inferred: the local user is the only tenant, and the
   secret store's namespace is that user's tenant, so the tenant is the namespace of the reference (``file:llm/provider``
   -> ``llm``; ``env:NAME`` -> ``default``). This rule gives no isolation between namespaces of one user's store, and
   claims none: it keeps the pre-0.3.15 behaviour of reading any reference the local user configured. Setting
   ``REDEVOPS_LOCAL_TENANT`` or passing ``tenant`` narrows reads and writes to one namespace; it does not cover ``env:``
   references. See docs/SECRETS.md, "Known limitations".

A refusal returns ``None`` and logs the reason code and the reference (a reference names a location and never holds a
value); no log line, return value or exception text of this module carries secret material."""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Optional, Tuple

from runtime_contracts.protocol.secrets import CredentialRequest, CredentialRequirement, SecretRef
from runtime_contracts.protocol.security import AuthorityContext, PrincipalRef
from runtime_contracts.secrets_local.broker import CredentialDenied

from .factory import build_credential_broker, build_secret_store


_log = logging.getLogger(__name__)

LOCAL_TENANT_ENV = "REDEVOPS_LOCAL_TENANT"
ENV_REFERENCE_TENANT = "default"      # the single-user tenant of an ``env:`` reference, which has no namespace


class SecretReferenceError(ValueError):
    pass


def parse_ref(ref: str) -> Tuple[str, str, str]:
    """``backend:namespace/path`` -> (backend, namespace, path). ``env:NAME`` -> ("env", "", "NAME")."""
    backend, sep, rest = (ref or "").strip().partition(":")
    # no backslash (a Windows path separator would step around the ``..`` check and the namespace binding) and no control, format or
    # separator character (log forging, invisible look-alikes): the reference is text an operator or a model proposes
    if not sep or not backend or not rest or any(c in ref for c in "\n\r\x00\\") or not ref.isprintable():
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


def _valid_tenant(tenant: Any) -> bool:
    """A tenant is a non-empty string with no surrounding White_Space and only printable characters. The contract only
    requires the two tenants to be equal, so a blank one on both sides would otherwise pass for an ``env:`` reference."""
    return isinstance(tenant, str) and bool(tenant) and tenant == tenant.strip() and tenant.isprintable()


def local_tenant(namespace: str, tenant: Optional[str] = None) -> Optional[str]:
    """The tenant a ``read_secret`` call acts for (the three sources are listed in the module docstring). Returns ``None``
    when the tenant is not usable, which the caller treats as a refusal: nothing here guesses a replacement."""
    if tenant is None:
        configured = os.environ.get(LOCAL_TENANT_ENV, "")
        tenant = configured if configured != "" else (namespace or ENV_REFERENCE_TENANT)
    return tenant if _valid_tenant(tenant) else None


def read_secret(ref: str, *, purpose: str = "llm-provider", capability: str = "llm.invoke",
                tenant: Optional[str] = None, mission: str = "llm", **store_cfg: Any) -> Optional[str]:
    """The secret's value for this one call, or ``None`` when the broker denies or the secret is absent (the caller
    then degrades; it never falls back to a different credential silently).

    ``tenant`` is the tenant the one-call service authority is issued for (see the module docstring). Omitted, the
    configured local tenant or the single-user rule applies; given, it is used as is and must equal the reference's
    namespace, or the contract refuses the request (``TENANT_MISMATCH``). A refusal logs its reason code."""
    backend, ns, path = parse_ref(ref)
    tenant = local_tenant(ns, tenant)
    if tenant is None:
        _log.warning("secret reference %s refused: TENANT_INVALID (the tenant is empty or malformed)", ref)
        return None
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
    except CredentialDenied as e:
        _log.warning("secret reference %s refused: %s", ref, e.reason)     # a DenyReason code, never material
        return None
    except Exception as e:  # noqa: BLE001 - SecretAccessError (absent / locked database) -> degrade
        _log.info("secret reference %s not read: %s", ref, type(e).__name__)  # the type only: a message may name a path
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


def store_secret(ref: str, value: str, *, tenant: Optional[str] = None, **store_cfg: Any) -> SecretRef:
    """Write ``value`` where ``ref`` points (create or replace). Returns the reference (never the value). The
    env backend is read-only: persist the key in a real store or keep using your exported variable.

    A write follows the same tenant as a read (``tenant`` argument, else ``REDEVOPS_LOCAL_TENANT``, else the reference's own
    namespace): a reference outside that tenant is refused and nothing is written."""
    backend, ns, path = parse_ref(ref)
    if backend != "env" and local_tenant(ns, tenant) != ns:
        _log.warning("secret reference %s not written: it is outside the tenant of this call", ref)
        raise SecretReferenceError("that reference is outside the tenant of this call (REDEVOPS_LOCAL_TENANT or the tenant argument); nothing was written")
    if backend == "env":
        raise SecretReferenceError("an env reference is read-only: export the variable yourself, or choose a store "
                                   "(file, encrypted-file, keyring, vault, keepassxc) to persist the key")
    if not value or "\x00" in value:
        raise SecretReferenceError("empty or invalid secret value")
    store = build_secret_store(backend, **store_cfg)
    return store.put(namespace=ns, path=path, value=value.encode("utf-8"))


__all__ = ["SecretReferenceError", "parse_ref", "read_secret", "store_secret"]
