"""Authenticated principals for operator actions (approve, reject, resume, compensate, reconcile).

The runtime used to record who acted as a free-text ``actor`` string chosen by the caller. It now takes an
:class:`AuthenticatedPrincipal`: an ``overlays.Principal`` that carries a proof, issued by the authentication layer, that
this identity passed through it. The runtime verifies the proof before it acts and writes the principal (id, kind, roles,
tenant, how it was authenticated) into the approval or resume record.

What exists underneath, unchanged: the control plane authenticates a bearer token into a ``control_auth.Caller`` (tenant,
role); the evidence plane's ``Authenticator`` returns a ``Principal`` for the single configured owner token. This module adds
no identity provider. :class:`PrincipalIssuer` signs "this principal was produced by the authentication layer of this
process" with an HMAC key held by the issuer (random per process unless ``AGENTIC_OS_PRINCIPAL_KEY`` is set).

Limits: the proof binds a principal to the authentication layer that issued it; it does not stop code running inside the
same process from calling the issuer directly (that code already holds the process's authority). It is not a signature a
third party can verify, and it does not survive a restart (a principal is re-authenticated per request).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field

from ..overlays import Principal

ENV_KEY = "AGENTIC_OS_PRINCIPAL_KEY"
#: tenant of a principal that may act for every tenant (the unscoped admin credential)
ALL_TENANTS = "*"


class PrincipalError(PermissionError):
    """The principal is missing, not authenticated, or does not match the mission it acts on."""


class PrincipalRequired(PrincipalError):
    """An operator action was attempted without an authenticated principal."""


@dataclass(frozen=True)
class AuthenticatedPrincipal(Principal):
    method: str = ""              # how it was authenticated, e.g. "control-token", "owner-token"
    issuer: str = ""
    issued_at: float = 0.0
    proof: str = field(default="", repr=False)

    def record(self) -> dict:
        """What is written to a mission log: the identity and how it was authenticated, never the proof."""
        return {"id": self.id, "kind": self.kind, "roles": list(self.roles), "tenant": self.tenant,
                "method": self.method, "issuer": self.issuer}


def _body(p: Principal, method: str, issuer: str, issued_at: float) -> bytes:
    return json.dumps({"id": p.id, "kind": p.kind, "roles": list(p.roles), "tenant": p.tenant, "method": method,
                       "issuer": issuer, "issued_at": issued_at}, sort_keys=True, separators=(",", ":")).encode("utf-8")


class PrincipalIssuer:
    def __init__(self, issuer_id: str = "control-plane", key: "bytes | None" = None) -> None:
        if not issuer_id:
            raise ValueError("an issuer needs an id")
        self.issuer_id = issuer_id
        self._key = key if key else os.urandom(32)

    def issue(self, principal: Principal, *, method: str) -> AuthenticatedPrincipal:
        if not method:
            raise ValueError("say how the principal was authenticated")
        if not getattr(principal, "id", ""):
            raise ValueError("a principal needs an id")
        at = time.time()
        proof = hmac.new(self._key, _body(principal, method, self.issuer_id, at), hashlib.sha256).hexdigest()
        return AuthenticatedPrincipal(id=principal.id, kind=principal.kind, roles=tuple(principal.roles), tenant=principal.tenant,
                                      method=method, issuer=self.issuer_id, issued_at=at, proof=proof)

    def verify(self, principal: object) -> bool:
        if not isinstance(principal, AuthenticatedPrincipal) or principal.issuer != self.issuer_id or not principal.proof:
            return False
        want = hmac.new(self._key, _body(principal, principal.method, principal.issuer, principal.issued_at), hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, principal.proof)


_DEFAULT: "PrincipalIssuer | None" = None


def default_issuer() -> PrincipalIssuer:
    """The process's authentication-layer issuer: used by the control-plane routes and, unless a runtime is given another
    verifier, by ``MissionRuntime``."""
    global _DEFAULT
    if _DEFAULT is None:
        raw = os.environ.get(ENV_KEY, "")
        _DEFAULT = PrincipalIssuer("control-plane", raw.encode("utf-8") if raw else None)
    return _DEFAULT


def principal_of_caller(caller) -> AuthenticatedPrincipal:
    """The principal behind an authenticated ``control_auth.Caller``: ``admin`` (all tenants), ``approver:<tenant>`` or
    ``requester:<tenant>``. Only call this with the result of ``resolve_caller`` (i.e. after the token was checked)."""
    if caller is None or getattr(caller, "anonymous", False):
        raise PrincipalRequired("no authenticated caller")
    tenant = getattr(caller, "tenant", None)
    role = getattr(caller, "role", "") or "admin"
    pid = "admin" if tenant is None else f"{role}:{tenant}"
    return default_issuer().issue(Principal(pid, "user", (role,), ALL_TENANTS if tenant is None else tenant), method="control-token")
