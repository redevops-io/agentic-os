"""Authorization policy for every state-changing route this app serves (see agentic_os/app_kit/route_authz.py).

Adding a write route without an entry here fails CI (apps/test_route_authz_registry.py) and is refused in production."""
from agentic_os.app_kit.route_authz import RoutePolicy as P

APP = "social-autopilot"

ROUTE_POLICIES = {
    "POST /api/reel/record": P("social-autopilot.reel_record", "authority_required"),
    "POST /api/reel/schedule": P("social-autopilot.reel_schedule", "authority_required"),
    "POST /api/vibex/trends": P("social-autopilot.vibex_trends", "read_only", rationale='fetches trend ideas; no state change'),
    "POST /api/vibex/generate": P("social-autopilot.vibex_generate", "authority_required"),
    "POST /api/vibex/draft": P("social-autopilot.vibex_draft", "authority_required"),
    "POST /agent/run": P("social-autopilot.agent_run", "stages_only", rationale='the core stages every consequential action as a preview unless it is handed an approval marker; the marker is earned only from an authenticated context, never the request body'),
    "POST /invoke": P("social-autopilot.invoke", "operator_boundary", rationale="enforced by the operator itself: a human approval or an execution authorization is required in production"),
}
