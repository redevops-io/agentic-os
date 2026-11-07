"""Operator identity for the AOS-DD runtime: the base ``AuthenticatedPrincipal`` and its control-plane credential path.

A free-text ``--actor`` is not an identity and is not accepted anywhere. Every operator action (submit, approve, revoke, resume,
reconcile, compensate) is bound to an ``AuthenticatedPrincipal`` issued by the base authentication layer
(``mission.principal.default_issuer`` through ``principal_of_caller``) after a bearer credential was checked with the base
``control_auth.resolve_caller`` (admin token, tenant token = ``requester``, approver token = ``approver``; constant-time compare,
distinct-token validation).

The base resolves credentials from the process environment (AGENTIC_OS_CONTROL_TOKEN / _TENANT_TOKENS / _APPROVER_TOKENS). For the
local CLI the expected tokens come from a 0600 *operator authentication file* instead (``OperatorAuth.from_file``); they are placed in
the environment only for the duration of the resolve call, under a lock, so the base code decides. In a real deployment the control
plane holds these tokens and the CLI would only present one; here both sides are one process.

Limits (documented, not hidden): the principal id is ``<role>:<tenant>`` (or ``admin``), i.e. one identity per credential, not per
person; the proof is the base in-process HMAC (a third party cannot verify it, it does not survive a restart: a principal is
re-authenticated per command); the file holding the expected tokens is a secret of the same trust zone as the application
credentials, but it is a DIFFERENT file and its values are never accepted as application credentials or approval keys.

Role -> action:  requester: submit, resume, reconcile   approver: approve, revoke, resume, compensate, redispatch, accept_uncertain   admin: all.
(`redispatch` and `accept_uncertain` are the only ways forward for an UNCERTAIN step whose provider does not enforce idempotency.)"""
from __future__ import annotations

import json
import os
import stat
import threading
from dataclasses import dataclass

from agentic_os.mission import control_auth as CA
from agentic_os.mission.principal import ALL_TENANTS, AuthenticatedPrincipal, PrincipalError, principal_of_caller

ACTIONS = ("submit", "approve", "revoke", "resume", "reconcile", "compensate", "redispatch", "accept_uncertain")
ROLE_ACTIONS = {"admin": set(ACTIONS), "approver": {"approve", "revoke", "resume", "compensate", "redispatch", "accept_uncertain"}, "requester": {"submit", "resume", "reconcile"}}
_ENV_LOCK = threading.Lock()
_ENV_KEYS = (CA.ENV_TOKEN, CA.ENV_TENANT_TOKENS, CA.ENV_APPROVER_TOKENS)


class AuthenticationFailed(PermissionError):
    pass


class NotPermitted(PermissionError):
    pass


@dataclass(frozen=True)
class OperatorAuth:
    """Expected operator credentials (what the control plane would hold)."""
    admin_token: str = ""
    tenant_tokens: "dict[str, str] | None" = None
    approver_tokens: "dict[str, str] | None" = None
    source: str = ""

    @classmethod
    def from_file(cls, path: str) -> "OperatorAuth":
        st = os.stat(path)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise AuthenticationFailed("the operator authentication file must be private (chmod 600)")
        d = json.load(open(path, encoding="utf-8"))
        if d.get("schema") != "aosdd.operators/1":
            raise AuthenticationFailed("not an aosdd.operators/1 file")
        return cls(admin_token=str(d.get("admin_token") or ""), tenant_tokens=dict(d.get("requester_tokens") or {}),
                   approver_tokens=dict(d.get("approver_tokens") or {}), source=os.path.basename(path))

    def authenticate(self, presented: str) -> AuthenticatedPrincipal:
        """The base ``resolve_caller`` decides; the principal is issued by the base issuer."""
        from fastapi import HTTPException  # noqa: PLC0415
        pairs = lambda m: ",".join(f"{k}={v}" for k, v in (m or {}).items())            # noqa: E731
        with _ENV_LOCK:
            saved = {k: os.environ.get(k) for k in _ENV_KEYS}
            try:
                os.environ[CA.ENV_TOKEN] = self.admin_token
                os.environ[CA.ENV_TENANT_TOKENS] = pairs(self.tenant_tokens)
                os.environ[CA.ENV_APPROVER_TOKENS] = pairs(self.approver_tokens)
                try:
                    caller = CA.resolve_caller("Bearer " + presented) if presented else None
                except (HTTPException, CA.ConfigError) as e:
                    raise AuthenticationFailed(f"operator credential refused ({getattr(e, 'status_code', 'config')})") from None
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        if caller is None:
            raise AuthenticationFailed("no operator credential presented")
        return principal_of_caller(caller)


def require(principal, action: str, tenant: str, verifier) -> AuthenticatedPrincipal:
    """The principal must be authenticated (base verifier), belong to the mission's tenant (or be the all-tenant admin) and hold a role
    that may ``action``."""
    if principal is None or not verifier.verify(principal):
        raise NotPermitted(f"{action}: an authenticated operator principal is required (a free-text actor is not an identity)")
    if principal.tenant not in (tenant, ALL_TENANTS):
        raise NotPermitted(f"{action}: the principal belongs to another tenant")
    if not any(action in ROLE_ACTIONS.get(r, ()) for r in principal.roles):
        raise NotPermitted(f"{action}: role {list(principal.roles)} may not {action}")
    return principal


class RoleCeiling:
    """The permission ceiling handed to the base (``grant_resume`` / ``compensate``): what the principal's ROLE may do."""

    def allows(self, principal, permission: str) -> bool:
        parts = str(permission).split(".")
        if len(parts) != 3:
            return False
        kind, _scope, action = parts
        if (kind, action) not in (("evidence", "resume"), ("mission", "compensate")):
            return False
        return any(action in ROLE_ACTIONS.get(r, ()) for r in getattr(principal, "roles", ()))
