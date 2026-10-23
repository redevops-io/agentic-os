"""Who may release a LIVE publish from the Projects/Content approve routes.

A route that can post to a real account must not trust the request body for "who approved". When the
publisher is live (anything but the simulated/no-op publishers) the caller must present a valid bearer token
(the same admin / tenant / approver credentials as the Mission API), and the recorded actor is derived from
that credential, never from the body. When an approver credential is configured
(``AGENTIC_OS_APPROVER_TOKENS``) a requester token may not approve (separation of duties). With a simulated
publisher nothing leaves the machine, so the demo UI keeps working without a token.
"""
from __future__ import annotations

from typing import Optional

SIMULATED_ACTOR = "owner"


def is_live_publisher(publisher) -> bool:
    """True when ``publisher`` can reach a real account. ``None`` means the service's own simulated default."""
    if publisher is None:
        return False
    from agentic_os.content.clients import FakePublisher, NoOpPublisher
    return not isinstance(publisher, (FakePublisher, NoOpPublisher))


def approving_actor(authorization: Optional[str], *, live: bool, claimed: str = "") -> str:
    """The actor to record for an approve/reject decision. Raises ``HTTPException`` (401/403/503) when a live
    publish is not authorised. For a simulated publisher returns the claimed actor (default ``owner``)."""
    if not live:
        return claimed or SIMULATED_ACTOR
    from fastapi import HTTPException
    from agentic_os.mission.control_auth import approver_tokens, resolve_caller
    caller = resolve_caller(authorization)             # 503 when no token is configured, 401 when missing/wrong
    if caller.role == "requester" and approver_tokens():
        raise HTTPException(403, "approval requires an approver credential")
    return "admin" if caller.role == "admin" else f"{caller.role}:{caller.tenant}"
