"""Authorization policy for every state-changing route this app serves (see agentic_os/app_kit/route_authz.py).

Adding a write route without an entry here fails CI (apps/test_route_authz_registry.py) and is refused in production."""
from agentic_os.app_kit.route_authz import RoutePolicy as P

APP = "outreach-engine"

ROUTE_POLICIES = {
    "POST /api/run": P("outreach-engine.run", "stages_only", rationale='the core stages every consequential action as a preview unless it is handed an approval marker; the marker is earned only from an authenticated context, never the request body'),
    "POST /agent/run": P("outreach-engine.agent_run", "stages_only", rationale='the core stages every consequential action as a preview unless it is handed an approval marker; the marker is earned only from an authenticated context, never the request body'),
    "POST /api/discover": P("outreach-engine.discover", "stages_only", rationale='the core stages every consequential action as a preview unless it is handed an approval marker; the marker is earned only from an authenticated context, never the request body'),
    "POST /api/agent": P("outreach-engine.agent", "stages_only", rationale='the core stages every consequential action as a preview unless it is handed an approval marker; the marker is earned only from an authenticated context, never the request body'),
    "POST /invoke": P("outreach-engine.invoke", "operator_boundary", rationale="enforced by the operator itself: a human approval or an execution authorization is required in production"),
}
