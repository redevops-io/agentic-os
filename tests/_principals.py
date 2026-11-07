"""Authenticated principals for tests: what the authentication layer would have issued after checking a credential."""
from __future__ import annotations

from agentic_os.mission.principal import default_issuer
from agentic_os.overlays import Principal


def authn(principal_or_id="owner", *, tenant: str = "*", roles=(), kind: str = "user", method: str = "test-login"):
    """An ``AuthenticatedPrincipal`` (all-tenant by default; pass ``tenant=``) for ``principal_or_id`` (a ``Principal`` or an id), issued by the process's default issuer."""
    p = principal_or_id if isinstance(principal_or_id, Principal) else Principal(str(principal_or_id), kind, tuple(roles), tenant)
    return default_issuer().issue(p, method=method)
