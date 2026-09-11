"""Gateway authentication — token → :class:`GatewayPrincipal` (plan §3.2).

The gateway trusts only a *verified identity + explicit scopes* (plan §7); it never trusts the
external agent's reasoning or a client-side claim. Verification is a **seam**: enterprise plugs a
real OAuth 2.1 + PKCE authorization server (and federated identity) in behind :class:`TokenVerifier`.

Open-core ships :class:`DevTokenVerifier` — a self-contained local issuer/verifier for development
and tests: opaque bearer tokens mapped to a principal, its workspace/scopes, and its granted
permissions, deny-by-default for anything it did not issue. It is NOT for production (no crypto,
no expiry, in-memory) — the docstring and name say so, and the enterprise verifier supersedes it.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol, Set, Tuple

from agentic_os.overlays import Principal

from .contracts import GatewayPrincipal


class GatewayAuthError(Exception):
    """Raised by a protocol adapter when a request carries no valid credential. Adapters map this
    to their transport's unauthorized response (e.g. HTTP 401); it never reveals capabilities."""


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Optional[GatewayPrincipal]: ...


def bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    """Extract the token from an ``Authorization: Bearer <token>`` header, or ``None``."""
    if not authorization_header:
        return None
    parts = authorization_header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


@dataclass
class DevTokenVerifier:
    """Local, in-memory dev auth. NOT FOR PRODUCTION.

    ``issue`` mints an opaque token bound to a principal (subject/tenant), its workspace, its OAuth
    scopes, and its granted permissions. ``verify`` resolves a token to its principal; ``authorize``
    (pass it as the gateway's authorizer) answers permission checks against the issued grants,
    deny-by-default. This lets a full governed MCP read path run end-to-end with no external IdP.
    """

    _principals: Dict[str, GatewayPrincipal] = field(default_factory=dict)      # token → principal
    _grants: Dict[Tuple[str, str], Set[str]] = field(default_factory=dict)      # (tenant,sub) → perms

    def issue(self, subject: str, *, tenant: str = "default", workspace: str = "",
              client_id: str = "dev-client", roles: Tuple[str, ...] = (),
              scopes: Tuple[str, ...] = (), grants: Tuple[str, ...] = ()) -> str:
        token = "gwt_" + secrets.token_urlsafe(24)
        gp = GatewayPrincipal(
            Principal(subject, "service", tuple(roles), tenant),
            client_id=client_id, workspace=workspace, scopes=tuple(scopes))
        self._principals[token] = gp
        self._grants.setdefault((tenant, subject), set()).update(grants)
        return token

    def verify(self, token: str) -> Optional[GatewayPrincipal]:
        return self._principals.get(token)                     # unknown token → None (deny)

    def authorize(self, principal: Principal, permission: str) -> bool:
        return permission in self._grants.get((principal.tenant, principal.id), set())
