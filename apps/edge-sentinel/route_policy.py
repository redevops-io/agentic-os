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

_B = "enforced by the route handler itself (apps/edge-sentinel/api_v1.py): a user with the required role (session cookie with CSRF token, or Basic), never a service or agent credential for a decision; the request body never names the actor"
ROUTE_POLICIES.update({
    "POST /api/v1/session": P("edge-sentinel.v1_login", "operator_boundary", rationale="sign-in: rate limited per client address and per user name; it grants nothing but a session"),
    "DELETE /api/v1/session": P("edge-sentinel.v1_logout", "operator_boundary", rationale=_B),
    "POST /api/v1/session/password": P("edge-sentinel.v1_password", "operator_boundary", rationale=_B + "; the current password is required"),
    "POST /api/v1/cases": P("edge-sentinel.v1_open_case", "operator_boundary", rationale=_B + "; this only STAGES a proposal that a person must still approve and execute"),
    "POST /api/v1/cases/{case_id}/actions/{request_id}/decision": P("edge-sentinel.v1_decide", "operator_boundary", rationale=_B + "; bound to the action digest"),
    "POST /api/v1/cases/{case_id}/actions/{request_id}/execute": P("edge-sentinel.v1_execute", "operator_boundary", rationale=_B + "; bound to the approved digest, then read back"),
    "POST /api/v1/users": P("edge-sentinel.v1_users", "operator_boundary", rationale=_B + " (admin)"),
    "PATCH /api/v1/users/{name}": P("edge-sentinel.v1_users", "operator_boundary", rationale=_B + " (admin)"),
    "POST /api/v1/users/{name}/password": P("edge-sentinel.v1_users", "operator_boundary", rationale=_B + " (admin)"),
    "POST /api/v1/actor-links": P("edge-sentinel.v1_actor_links", "operator_boundary", rationale=_B + " (admin)"),
    "DELETE /api/v1/actor-links/{external_actor}": P("edge-sentinel.v1_actor_links", "operator_boundary", rationale=_B + " (admin)"),
})
ROUTE_POLICIES.update({
    "POST /api/v1/hunt/validate": P("edge-sentinel.v1_hunt_validate", "read_only", rationale="pure validation of a query string; nothing is executed; needs a credential like every /api/v1 route"),
    "POST /api/v1/ioc/normalize": P("edge-sentinel.v1_ioc_normalize", "read_only", rationale="pure normalisation of an indicator string; no network, no state"),
    "POST /api/v1/detections/validate": P("edge-sentinel.v1_detections_validate", "read_only", rationale="structural validation of a rule; nothing is stored, published or deployed"),
    "POST /api/v1/agent-events": P("edge-sentinel.v1_agent_events", "operator_boundary", rationale=_B + "; a SIGNAL: opens an INVESTIGATING case, proposes no action"),
})
