"""Authorization policy for every state-changing route the Projects API serves (see agentic_os/app_kit/route_authz.py; audit finding F22).

The Projects API is a demo surface by default: its "identity" is a demo session or the socket peer and it ships over a sample provider.
In the production profile the same enforcement layer the apps use refuses every write route that has no policy and every call that does
not carry an authenticated context, BEFORE the handler runs. Outside production nothing is blocked (the demo keeps working).

Adding a write route without an entry here fails CI (tests/test_projects_route_authz.py) and is refused in production.

Access classes: a decision a person makes (approve, reconcile, connect an app, confirm sources, promote policy, a batch of approvals,
the demo's simulated outcome) is `human_only`: an agent or a service never stands in for it. Everything else that changes state needs an
authenticated human with the permission, an agent's execution authorization, or a service capability. A pure query is `read_only`.
"""
from __future__ import annotations

from agentic_os.app_kit.route_authz import RoutePolicy as P

APP = "projects"
PREFIX = "/api/projects/{project_id}"

PROJECTS_ROUTE_POLICIES = {
    "POST /api/apps/{provider}/connect": P("projects.connect_app", "human_only", rationale="connecting an external app is a person's decision"),
    "POST /api/apps/{provider}/connect/start": P("projects.connect_app_start", "human_only", rationale="starts an external authorization a person completes"),
    "POST /api/outcomes": P("projects.record_outcome", "authority_required"),
    "POST /api/sidekick": P("projects.sidekick", "authority_required"),
    "POST /api/sources/propose": P("projects.propose_sources", "read_only", rationale="proposes sources from text; nothing is connected"),
    "POST /api/sources/confirm": P("projects.confirm_sources", "human_only", rationale="confirming what the system may read is a person's decision"),
    f"POST {PREFIX}/capture": P("projects.capture", "authority_required"),
    f"POST {PREFIX}/capture/{{capture_id}}/resolve": P("projects.capture_resolve", "authority_required"),
    f"POST {PREFIX}/capture/{{capture_id}}/archive": P("projects.capture_archive", "authority_required"),
    f"POST {PREFIX}/policy/promotion-cycle": P("projects.policy_promotion_cycle", "human_only", rationale="promoting a policy is a governance decision"),
    f"POST {PREFIX}/today/batch": P("projects.today_batch", "human_only", rationale="a batch can approve many items at once"),
    f"POST {PREFIX}/today/batch/undo": P("projects.today_batch_undo", "authority_required"),
    f"POST {PREFIX}/today/notifications/ack": P("projects.today_ack", "authority_required"),
    f"PUT {PREFIX}/today/notification-prefs": P("projects.today_prefs", "authority_required"),
    f"POST {PREFIX}/today/{{item_id}}/approve": P("projects.today_approve", "human_only", rationale="approval is a person's decision"),
    f"POST {PREFIX}/today/{{item_id}}/reconcile": P("projects.today_reconcile", "human_only", rationale="resolving an unknown outcome needs a named person and evidence"),
    f"POST {PREFIX}/today/{{item_id}}/snooze": P("projects.today_snooze", "authority_required"),
    f"POST {PREFIX}/today/{{item_id}}/dismiss": P("projects.today_dismiss", "authority_required"),
    f"POST {PREFIX}/today/{{item_id}}/simulate-outcome": P("projects.today_simulate_outcome", "human_only", rationale="a demo-only simulation of an outcome; never an agent's call"),
}
