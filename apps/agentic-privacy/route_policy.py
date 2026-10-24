"""Authorization policy for every state-changing route this app serves (see agentic_os/app_kit/route_authz.py).

Adding a write route without an entry here fails CI (apps/test_route_authz_registry.py) and is refused in production."""
from agentic_os.app_kit.route_authz import RoutePolicy as P

APP = "agentic-privacy"

ROUTE_POLICIES = {
    "POST /api/agent": P("agentic-privacy.agent", "stages_only", rationale='the core stages every consequential action as a preview unless it is handed an approval marker; the marker is earned only from an authenticated context, never the request body'),
    "POST /request": P("agentic-privacy.request", "public_intake", rationale='data-subject request form: rate-limited, opens a PENDING record only; nothing happens until the emailed token is redeemed'),
    "POST /intake": P("agentic-privacy.intake", "public_intake", rationale='data-subject intake form: rate-limited, per-address capped, opens a PENDING record only'),
    "POST /verify": P("agentic-privacy.verify", "public_intake", rationale='redeems the emailed single-use token that proves identity; the token is the credential'),
    "POST /fulfil/{request_id}": P("agentic-privacy.fulfil", "human_only"),
    "POST /retention": P("agentic-privacy.retention", "stages_only", rationale='unauthenticated callers get a counts-only dry run; a purge needs the operator credential and confirm:true (enforced in the route)'),
    "POST /invoke": P("agentic-privacy.invoke", "operator_boundary", rationale="enforced by the operator itself: a human approval or an execution authorization is required in production"),
}
