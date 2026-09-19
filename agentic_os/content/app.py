"""Assemble the Projects API/UI over live Content Missions.

`create_content_app(registry)` builds the standard Projects app (UI mounted, `/api/...` projections) but
backed by a `ContentProjectionProvider`, and adds the governed approve endpoint the UI's approval action
calls. Approval routes to `registry.approve`, which resolves that mission's publish gate via the Mission
Runtime — so publishing (the one side-effecting step) happens only on the owner's approval.
"""
from __future__ import annotations

from .projection import ContentMissionRegistry, ContentProjectionProvider


def create_content_app(registry: ContentMissionRegistry):
    """A FastAPI app serving the Projects UI + content-mission projections + the approve action."""
    from ..projects_api import create_app

    app = create_app(ContentProjectionProvider(registry))

    @app.post("/api/projects/{project_id}/missions/{mission_id}/approve")
    def _approve(project_id: str, mission_id: str, body: dict | None = None):
        # the governed owner decision from the Projects UI → resolve the mission's publish gate
        decision = (body or {}).get("decision", "approve").lower()
        result = registry.reject(mission_id) if decision == "reject" else registry.approve(mission_id)
        if result is None:
            return {"ok": False, "error": "mission not found"}
        return {"ok": True, "decision": decision, "mission_state": result.mission_state,
                "drafts": [{"channel": d.channel.value, "status": d.status, "post_url": d.post_url}
                           for d in result.drafts]}

    # create_app mounts the static UI at "/" (a catch-all) before this route was added; move any such
    # mounts to the end so the approve API route is matched first.
    from starlette.routing import Mount
    mounts = [r for r in app.router.routes if isinstance(r, Mount)]
    for m in mounts:
        app.router.routes.remove(m)
        app.router.routes.append(m)

    return app
