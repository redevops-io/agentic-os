"""Who may release a LIVE publish from the Projects/Content approve routes.

A route that can post to a real account must not trust the request body for "who approved". When the
publisher is live (anything but the simulated/no-op publishers) the caller must present a valid bearer token
(the same admin / tenant / approver credentials as the Mission API), and the recorded actor is derived from
that credential, never from the body. When an approver credential is configured
(``AGENTIC_OS_APPROVER_TOKENS``) a requester token may not approve (separation of duties). With a simulated
publisher nothing leaves the machine, so the demo UI keeps working without a token.
"""
from __future__ import annotations

from typing import Iterable, Optional

SIMULATED_ACTOR = "owner"
PUBLISH_GRANT = "content.publish"


def is_live_publisher(publisher) -> bool:
    """True when ``publisher`` can reach a real account. ``None`` means the service's own simulated default."""
    if publisher is None:
        return False
    from agentic_os.content.clients import FakePublisher, NoOpPublisher
    return not isinstance(publisher, (FakePublisher, NoOpPublisher))


def approving_actor(authorization: Optional[str], *, live: bool, claimed: str = "", tenant: str = "",
                    channels: Iterable[str] = (), publishing: bool = True) -> str:
    """The actor to record for an approve/reject decision. Raises ``HTTPException`` (401/403/503) when a live
    publish is not authorised. For a simulated publisher returns the claimed actor (default ``owner``).

    Authentication is not authorisation. For a TENANT-scoped credential (tenant or approver token) this also checks
    that (a) the credential's tenant owns this mission/account (``tenant``; an unowned service refuses every
    tenant-scoped caller: fail closed), and (b) when ``publishing``, the tenant has been granted ``content.publish``
    or ``content.publish.<channel>`` for EVERY channel being published (``AGENTIC_OS_TENANT_LIMITS`` policy_refs, the
    same grants the Mission API uses). Only the unscoped admin token is global."""
    if not live:
        return claimed or SIMULATED_ACTOR
    from fastapi import HTTPException
    from agentic_os.mission.control_auth import approver_tokens, resolve_caller, tenant_limits
    caller = resolve_caller(authorization)             # 503 when no token is configured, 401 when missing/wrong
    if caller.role == "requester" and approver_tokens():
        raise HTTPException(403, "approval requires an approver credential")
    if caller.tenant is not None:
        if not tenant or caller.tenant != tenant:
            raise HTTPException(403, "this credential is not authorised for that account")
        if publishing:
            granted = set(tenant_limits(caller.tenant)[0])
            for ch in channels:
                if PUBLISH_GRANT not in granted and f"{PUBLISH_GRANT}.{ch}" not in granted:
                    raise HTTPException(403, f"this credential may not publish to {ch}")
    return "admin" if caller.role == "admin" else f"{caller.role}:{caller.tenant}"
