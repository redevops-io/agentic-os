"""Authorization policy for every state-changing route this app serves (see agentic_os/app_kit/route_authz.py).

Adding a write route without an entry here fails CI (apps/test_route_authz_registry.py) and is refused in production."""
from agentic_os.app_kit.route_authz import RoutePolicy as P

APP = "growth-assistant"

ROUTE_POLICIES = {
    "POST /api/chat": P("growth-assistant.chat", "stages_only", rationale='public chat: it may start in-app content generation, never an external action; consequential actions stay staged by the core and this route injects no approval marker'),
    "POST /agent/run": P("growth-assistant.agent_run", "stages_only", rationale='the core stages every consequential action as a preview unless it is handed an approval marker; the marker is earned only from an authenticated context, never the request body'),
    "POST /invoke": P("growth-assistant.invoke", "operator_boundary", rationale="enforced by the operator itself: a human approval or an execution authorization is required in production"),
}
