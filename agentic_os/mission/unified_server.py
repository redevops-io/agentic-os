"""Unified desktop — the thin control-plane server for the P2 vertical slice.

Exposes exactly the surfaces the minimal desktop shell needs — Sidekick, Needs You, Mission progress,
Evidence, Action receipt, Connections — over the two-domain governed Mission from ``unified_demo``. It adds
NO business logic: state-changing work stays in the Mission Runtime; this is a read/drive BFF.

Credential invisibility is enforced here by omission — no endpoint returns a token or reads one into a
response. The shell only ever learns "CRM — Connected" / "Support — Connected" via ``/api/connections``.

Run:  uvicorn agentic_os.mission.unified_server:app  (needs TWENTY_API_KEY + CHATWOOT_API_TOKEN in env,
resolved only through the broker; point *_BASE_URL at real cores or leave them for the in-memory projection).
"""
# NB: no `from __future__ import annotations` — FastAPI must see real annotation objects (Request, path
# params) on the endpoint functions defined inside _build_app(), not PEP 563 string forward-refs.
import os
import tempfile
from typing import Any, Dict, List

from .receipt import mission_receipt
from .types import MissionState
from .unified_demo import build_unified_runtime, connections_view, seed_mission

# One long-lived runtime over a persistent JSONL store (restart durability is proven in the headless test;
# a running server just keeps the one runtime).
_STORE = os.environ.get("UNIFIED_STORE") or os.path.join(tempfile.gettempdir(), "unified_desktop_events.jsonl")
_RT, _ = build_unified_runtime(_STORE)
_MISSIONS: List[str] = []

# The deployed agents the bridge reaches (same URL map the console uses). Empty in local/demo mode → the
# bridge simply finds no deployed capabilities; set these to point one Sidekick UI at all deployed apps.
_AGENT_URLS: Dict[str, str] = {k: v for k, v in {
    "revenue": os.environ.get("REVENUE_URL", ""), "intelligence": os.environ.get("INTEL_URL", ""),
    "content": os.environ.get("CONTENT_URL", ""), "security": os.environ.get("SEC_URL", ""),
    "finance": os.environ.get("FINANCE_URL", ""), "customer-success": os.environ.get("CS_URL", ""),
}.items() if v}


def _mission_head(mid: str) -> Dict[str, Any]:
    state = _RT.repo.state(mid)
    return {
        "id": mid,
        "goal": _RT.repo.goal(mid),
        "state": state.value if isinstance(state, MissionState) else state,
        "pending_human": _RT.repo.pending_human(mid),
        "node_status": {k: (v.value if hasattr(v, "value") else v)
                        for k, v in (_RT.repo.node_status(mid) or {}).items()},
        "results": _RT.repo.node_results(mid),
    }


def _needs_you() -> List[Dict[str, Any]]:
    """Fold every parked mission into one canonical attention list (source, mission, capability, decisions)."""
    items: List[Dict[str, Any]] = []
    for mid in _MISSIONS:
        pend = _RT.repo.pending_human(mid)
        if not pend:
            continue
        items.append({
            "mission_id": mid, "node_id": pend.get("node_id"), "capability": pend.get("capability"),
            "title": f"Approve: {pend.get('capability')}", "why": pend.get("prompt")
            or "A consequential cross-domain action needs your sign-off.",
            "goal": _RT.repo.goal(mid), "decisions": ["approve", "reject"],
            "evidence": pend.get("evidence"),
        })
    return items


def _build_app():
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="ReDevOps Unified Desktop (P2 slice)")

    @app.get("/api/connections")
    def connections():
        return connections_view(_RT)

    @app.post("/api/sidekick")
    async def sidekick(request: Request):
        """Minimal NL-intent surface: a cross-domain sync intent seeds the governed two-domain Mission."""
        body = await request.json()
        t = str(body.get("text") or "").lower()
        if any(w in t for w in ("sync", "support", "account", "pilot", "onboard", "revenue")):
            m = seed_mission(_RT)
            _MISSIONS.append(m.id)
            state = _RT.repo.state(m.id)
            return {"reply": "Started a governed Mission: read the CRM account, then sync it into Support. "
                             "It's paused for your approval before the Support write.",
                    "mission_id": m.id, "state": state.value if isinstance(state, MissionState) else state,
                    "actions": [{"label": "Review approval", "kind": "open_needs_you"}]}
        return {"reply": "Try: \"sync the pilot account from revenue into support\".",
                "mission_id": None, "actions": []}

    @app.get("/api/needs-you")
    def needs_you():
        return {"items": _needs_you()}

    @app.get("/api/missions/{mid}")
    def mission(mid: str):
        if _RT.repo.state(mid) is None:
            raise HTTPException(404, "unknown mission")
        return _mission_head(mid)

    @app.get("/api/missions/{mid}/events")
    def events(mid: str):
        return {"events": _RT.repo.timeline(mid)}

    @app.get("/api/missions/{mid}/evidence")
    def evidence(mid: str):
        return {"evidence": _RT.evidence.ledger(mid)}

    @app.get("/api/missions/{mid}/receipt")
    def receipt(mid: str):
        return mission_receipt(_RT, mid)

    @app.post("/api/missions/{mid}/approve")
    async def approve(mid: str, request: Request):
        if _RT.repo.state(mid) is None:
            raise HTTPException(404, "unknown mission")
        body = await request.json()
        _RT.approve(mid, body.get("node_id"), body.get("decision", "approve"))
        return _mission_head(mid)

    # ── bridge: one control surface over the DEPLOYED agents (their /capabilities + /invoke) ──────────
    @app.get("/api/bridge/capabilities")
    def bridge_capabilities():
        """List every capability the deployed agents publish — so the UI shows all apps' commands at once."""
        from .bridge import discover_capabilities
        caps, _bases = discover_capabilities(_AGENT_URLS)
        return {"agents": sorted(set(_AGENT_URLS)), "capabilities": [c.as_dict() for c in caps]}

    @app.post("/api/bridge/invoke")
    async def bridge_invoke(request: Request):
        """Issue a real command to a deployed agent (credential-free — the agent resolves its own creds)."""
        from .bridge import BridgeError, build_bridge
        body = await request.json()
        _caps, client = build_bridge(_AGENT_URLS)
        try:
            result = client.invoke(str(body.get("operator", "")), str(body.get("capability", "")),
                                   body.get("inputs") or {}, str(body.get("idempotency_key", "")))
        except BridgeError as e:
            raise HTTPException(502, str(e))
        return {"result": result}

    @app.get("/", response_class=HTMLResponse)
    def shell():
        from .unified_shell import SHELL_HTML
        return HTMLResponse(SHELL_HTML)

    return app


app = _build_app()
