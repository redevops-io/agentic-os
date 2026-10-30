"""Authentication for the ``/v1/intelligence`` router: the caller is authenticated, and the TENANT comes from the principal.

``build_router(service)`` with no authenticator is the unauthenticated demo (the request body's ``tenant`` is used as
sent). Anything that serves real data must pass an authenticator::

    router = build_router(service, authenticator=StaticBearerAuthenticator({token: ("svc-a", "tenant-a")}))

With an authenticator the router:

* answers 401 to a request without a credential the authenticator accepts;
* sets the tenant of every :class:`DecisionNeed` from the authenticated principal, never from the body;
* answers 403 when the body names a different tenant than the principal's (a caller-supplied tenant is not an
  authorisation input), and 404 for a stored result that belongs to another tenant.

An :class:`IntelligenceAuthenticator` is any object with ``authenticate(authorization_header) -> principal | None``.
:class:`StaticBearerAuthenticator` (a fixed token table) and :class:`GatewayVerifierAuthenticator` (any Agent Gateway
``TokenVerifier``, for example the OAuth 2.1 one) are provided. Rate limiting, credential issuance and rotation are
not part of this module.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Tuple


@dataclass(frozen=True)
class IntelligencePrincipal:
    """An authenticated caller: who it is and the one tenant it may act in."""
    subject: str
    tenant: str


class IntelligenceAuthenticator(Protocol):
    def authenticate(self, authorization: Optional[str]) -> Optional[IntelligencePrincipal]: ...


def _bearer(authorization: Optional[str]) -> Optional[str]:
    from ..agent_gateway.auth import bearer_token  # noqa: PLC0415
    return bearer_token(authorization)


class StaticBearerAuthenticator:
    """A fixed table ``token -> (subject, tenant)``. Each presented token is compared against every configured
    token with ``hmac.compare_digest`` (no early exit, no dictionary lookup keyed by the secret)."""

    def __init__(self, tokens: Mapping[str, Tuple[str, str]]) -> None:
        table = []
        for token, (subject, tenant) in tokens.items():
            if not token or not subject or not tenant or tenant != tenant.strip():
                raise ValueError("a token, a subject and a non-empty tenant (no surrounding whitespace) are required")
            table.append((token.encode("utf-8"), IntelligencePrincipal(subject, tenant)))
        if not table:
            raise ValueError("at least one token is required")
        self._table = tuple(table)

    def authenticate(self, authorization: Optional[str]) -> Optional[IntelligencePrincipal]:
        presented = _bearer(authorization)
        if not presented:
            return None
        candidate = presented.encode("utf-8")
        found: Optional[IntelligencePrincipal] = None
        for token, principal in self._table:
            if hmac.compare_digest(candidate, token):
                found = principal
        return found


class GatewayVerifierAuthenticator:
    """Adapts an Agent Gateway ``TokenVerifier`` (token -> ``GatewayPrincipal``): the principal's tenant is the
    tenant of the call."""

    def __init__(self, verifier) -> None:
        self._verifier = verifier

    def authenticate(self, authorization: Optional[str]) -> Optional[IntelligencePrincipal]:
        presented = _bearer(authorization)
        if not presented:
            return None
        gp = self._verifier.verify(presented)
        if gp is None:
            return None
        return IntelligencePrincipal(gp.subject, gp.tenant)
