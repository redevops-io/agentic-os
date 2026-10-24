"""Authorization policy for every state-changing route this app serves (see agentic_os/app_kit/route_authz.py).

Adding a write route without an entry here fails CI (apps/test_route_authz_registry.py) and is refused in production."""
from agentic_os.app_kit.route_authz import RoutePolicy as P

APP = "billing"

ROUTE_POLICIES = {
    "POST /api/agent": P("billing.agent", "stages_only", rationale='the core stages every consequential action as a preview unless it is handed an approval marker; the marker is earned only from an authenticated context, never the request body'),
    "POST /agent/run": P("billing.agent_run", "stages_only", rationale='the core stages every consequential action as a preview unless it is handed an approval marker; the marker is earned only from an authenticated context, never the request body'),
    "POST /api/outcomes/observe": P("billing.outcomes_observe", "authority_required"),
    "POST /api/policy/refit": P("billing.policy_refit", "authority_required"),
    "POST /api/decide": P("billing.decide", "authority_required"),
    "POST /api/decide/{mission_id}/approve": P("billing.decide_approve", "human_only"),
    "POST /invoke": P("billing.invoke", "operator_boundary", rationale="enforced by the operator itself: a human approval or an execution authorization is required in production"),
}
