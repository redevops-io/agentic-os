"""Authorization policy for every state-changing route this app serves (see agentic_os/app_kit/route_authz.py).

Adding a write route without an entry here fails CI (apps/test_route_authz_registry.py) and is refused in production."""
from agentic_os.app_kit.route_authz import RoutePolicy as P

APP = "document-editor"

ROUTE_POLICIES = {
    "POST /invoke": P("document-editor.invoke", "operator_boundary", rationale="enforced by the operator itself: a human approval or an execution authorization is required in production"),
}
