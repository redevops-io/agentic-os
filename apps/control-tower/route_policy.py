"""Authorization policy for every state-changing route this app serves (see agentic_os/app_kit/route_authz.py).

Adding a write route without an entry here fails CI (apps/test_route_authz_registry.py) and is refused in production."""
from agentic_os.app_kit.route_authz import RoutePolicy as P

APP = "control-tower"

ROUTE_POLICIES = {
    "POST /agent/run": P("control-tower.agent_run", "read_only", rationale='actions are ask and refresh only; both read'),
    "POST /invoke": P("control-tower.invoke", "operator_boundary", rationale="enforced by the operator itself: a human approval or an execution authorization is required in production"),
}
