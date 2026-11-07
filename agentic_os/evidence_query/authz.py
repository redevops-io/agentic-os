"""Authorization for evidence operations, on the repo's ordinary permission plane.

Scope permissions are plain grants ``evidence.<scope>.<action>`` (resource kind ``tool``, action ``read``), evaluated by the
SAME ceiling pattern the app directory uses (:class:`IdentityCeiling` = the process identity provider, deny by default;
:class:`GrantsCeiling` = the encrypted grant store). The AGPL core therefore enforces single-owner access through the
ordinary plane, and the Enterprise multi-user / row / column / document-level plane plugs in by replacing the ceiling
(``overlays.set_identity_provider``) - no evidence-specific permission system exists.

Actions (disjoint names; a dotted-prefix grant ``evidence.<scope>`` covers all four, ``evidence`` covers every scope):
``inspect`` (read evidence through the query service), ``reconstruct`` (rebuild a historical state, no side effects),
``resume`` (continue a reconstructed mission under currently valid approvals), ``reexecute`` (re-plan and run again)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Any, Optional, Protocol

from ..overlays import Principal

_log = logging.getLogger("agentic_os.evidence_query")
ACTIONS = ("inspect", "reconstruct", "resume", "reexecute")
_SCOPE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def perm(scope: str, action: str) -> str:
    if action not in ACTIONS or not isinstance(scope, str) or not _SCOPE.match(scope):
        raise ValueError("invalid permission")
    return f"evidence.{scope}.{action}"


class Ceiling(Protocol):
    def allows(self, principal: Any, permission: str) -> bool: ...


class IdentityCeiling:
    """The process identity plane (deny by default; the Enterprise plane replaces it)."""

    def allows(self, principal: Any, permission: str) -> bool:
        from .. import overlays
        return bool(overlays.identity_provider().authorize(principal, permission))


class GrantsCeiling:
    """The grant store: ``<user|role>`` may ``read`` the ``tool`` resource ``evidence.<scope>.<action>``. A sealed store
    or any error denies."""

    def __init__(self, plane: Any) -> None:
        self.plane = plane

    def allows(self, principal: Any, permission: str) -> bool:
        from ..permissions import Identity
        ident = Identity(app="", user=str(getattr(principal, "id", "") or ""), roles=list(getattr(principal, "roles", ()) or ()))
        return bool(self.plane.authorize(ident, "tool", permission, "read").allowed)


def allowed(ceiling: Ceiling, principal: Any, permission: str) -> bool:
    if principal is None:
        return False
    try:
        return bool(ceiling.allows(principal, permission))
    except Exception:                                      # noqa: BLE001 - a broken ceiling must deny, never allow
        _log.warning("evidence permission ceiling failed while checking %s: denying", permission)
        return False


# ───────────────────────── authentication (single owner; Enterprise replaces it) ─────────────────────────
class Authenticator(Protocol):
    def authenticate(self, credential: Optional[str]) -> Optional[Principal]: ...


class SingleOwnerTokenAuthenticator:
    """AGPL core: one configured bearer token = the owner. Compared in constant time on SHA-256 digests. A token shorter
    than 24 characters is refused at construction (no guessable default). Multi-user identity (SSO/OIDC, per-user tokens,
    revocation) is the Enterprise identity plane behind the same ``Authenticator`` protocol."""

    MIN_LEN = 24

    def __init__(self, token: str, principal_id: str = "owner", roles: tuple = ()):
        if not isinstance(token, str) or len(token) < self.MIN_LEN:
            raise ValueError(f"the owner token must be at least {self.MIN_LEN} characters")
        self._digest = hashlib.sha256(token.encode()).digest()
        self.principal = Principal(principal_id, "user", tuple(roles))

    def authenticate(self, credential: Optional[str]) -> Optional[Principal]:
        if not credential:
            return None
        cand = hashlib.sha256(credential.encode()).digest()
        if not hmac.compare_digest(cand, self._digest):
            return None
        from ..mission.principal import default_issuer          # noqa: PLC0415 - the principal carries the proof that it was authenticated here
        return default_issuer().issue(self.principal, method="owner-token")


class StaticCeiling:
    """An explicit, in-code single-owner ceiling (demo / local tools / tests): allows a permission iff it equals or is
    dotted-prefix-covered by one of ``grants``. Never a default anywhere; callers construct it deliberately."""

    def __init__(self, grants):
        self.grants = frozenset(grants)

    def allows(self, principal: Any, permission: str) -> bool:
        return any(permission == g or permission.startswith(g + ".") for g in self.grants)
