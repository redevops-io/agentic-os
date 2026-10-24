"""Authorization policy for every state-changing route this app serves (see agentic_os/app_kit/route_authz.py).

Adding a write route without an entry here fails CI (apps/test_route_authz_registry.py) and is refused in production."""
from agentic_os.app_kit.route_authz import RoutePolicy as P

APP = "edge-sentinel"

ROUTE_POLICIES = {
    "POST /api/agent": P("edge-sentinel.agent", "stages_only", rationale='the core stages every consequential action as a preview unless it is handed an approval marker; the marker is earned only from an authenticated context, never the request body'),
    "POST /api/permissions/query": P("edge-sentinel.permissions_query", "read_only", rationale='runs a read-only vulnerability query for the permissions demo'),
    "POST /api/cases/{case_id}/respond": P("edge-sentinel.case_respond", "human_only"),
    "POST /api/cases/{case_id}/execute": P("edge-sentinel.case_execute", "human_only"),
    "POST /agent/run": P("edge-sentinel.agent_run", "stages_only", rationale='the core stages every consequential action as a preview unless it is handed an approval marker; the marker is earned only from an authenticated context, never the request body'),
    "POST /invoke": P("edge-sentinel.invoke", "operator_boundary", rationale="enforced by the operator itself: a human approval or an execution authorization is required in production"),
}
