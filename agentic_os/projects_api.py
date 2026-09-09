"""Projects API — the thin read/action HTTP surface the Projects UI (redevops-projects)
renders.

It serves **UI-facing projections** (overview · missions · workflows · attention ·
discovery · apps · activity) at the exact paths the front-end's ``HttpDataClient`` calls,
plus a ``POST /api/sidekick`` for the conversational surface. Each projection carries
provenance (``source_runtime`` + ``source_refs``) so advanced views can drill down without
the UI knowing Runtime internals — Runtime storage can evolve without a frontend rewrite.

Projections come from a pluggable :class:`ProjectionProvider`. A :class:`SampleProjectionProvider`
ships so the API (and the UI against it) runs end-to-end today; bind real Runtime stores by
implementing the same protocol. The **apps** projection prefers the live connector setup
guides (``redevops_connectors.SETUP_GUIDES`` + ``verify_setup``) when the ``[connectors]``
plugin is installed, and falls back to the sample otherwise — so "what apps can this Project
use, and are they connected?" reflects the real Integration Plane when it's present.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
except ImportError as e:  # pragma: no cover
    raise ImportError("the Projects API needs fastapi (a base dependency)") from e


def _prov(runtime: str, *refs: str) -> Dict[str, Any]:
    return {"source_runtime": runtime, "source_refs": list(refs)}


# ── the projection contract ─────────────────────────────────────────────────────
class ProjectionProvider(Protocol):
    def projects(self) -> List[dict]: ...
    def overview(self, project_id: str) -> dict: ...
    def missions(self, project_id: str) -> List[dict]: ...
    def workflows(self, project_id: str) -> List[dict]: ...
    def attention(self, project_id: str) -> List[dict]: ...
    def discovery(self, project_id: str) -> List[dict]: ...
    def apps(self, project_id: str) -> List[dict]: ...
    def activity(self, project_id: str) -> List[dict]: ...


@dataclass
class SampleProjectionProvider:
    """Deterministic projections matching the UI's shapes — the API runs end-to-end with no
    Runtime wiring. Replace method-by-method with real projections; each already carries the
    provenance the UI drill-down expects."""

    project_id: str = "customer-ops"
    project_name: str = "Customer Operations"

    def projects(self) -> List[dict]:
        return [{"id": self.project_id, "name": self.project_name, "health": "ok"}]

    def missions(self, _pid: str) -> List[dict]:
        m = [
            ("4821", "Refund Sarah Chen", "Customer Refunds", "needs", "4/7", "mission:4821"),
            ("triage", "Daily support triage", "Support Triage", "running", "84%", "mission:triage"),
            ("vti", "Review VTI exposure", "Portfolio Review", "completed", "Verified", "mission:vti"),
            ("rel24", "Deploy release 2.4", "Release Workflow", "failed", "Verify step", "mission:rel24"),
            ("prospect", "Weekly prospecting", "Prospecting", "scheduled", "—", "mission:prospect"),
        ]
        return [{"id": i, "title": t, "workflow": w, "state": s, "progress": p, **_prov("mission", r)}
                for i, t, w, s, p, r in m]

    def workflows(self, _pid: str) -> List[dict]:
        return [
            {"id": "refund", "name": "Customer Refund Handling",
             "steps": ["Find customer", "Find charge", "Request approval", "Refund", "Verify", "Reply"],
             "cadence": "Event-driven", "state": "active",
             "runsNote": "12 runs · 11 completed · 1 needs attention", **_prov("mission", "workflow:refund")},
            {"id": "triage", "name": "Daily Support Triage",
             "steps": ["Ingest", "Classify", "CRM", "Route", "Escalate"], "cadence": "Weekdays 08:00",
             "state": "scheduled", "runsNote": "runs daily", **_prov("mission", "workflow:triage")},
            {"id": "prospect", "name": "Weekly Prospecting",
             "steps": ["Discover", "Enrich", "Sequence", "Approve", "Activate"], "cadence": "Weekly",
             "state": "paused", "runsNote": "approval before activation", **_prov("mission", "workflow:prospect")},
        ]

    def attention(self, _pid: str) -> List[dict]:
        return [
            {"id": "a1", "kind": "approval", "title": "Approve refund · $129",
             "reason": "Sarah Chen · duplicate charge", "consequence": "Executes a Stripe refund (tier 4)",
             "available_actions": ["Review", "Approve", "Reject"], "priority": 90, **_prov("governance", "mission:4821")},
            {"id": "a2", "kind": "reconnect", "title": "Reconnect WhatsApp", "reason": "Token expires soon",
             "consequence": "Customer channel stops receiving", "available_actions": ["Connect"], "priority": 70,
             **_prov("connector", "app:whatsapp_business")},
            {"id": "a3", "kind": "discovery_review", "title": "Refund policy changed",
             "reason": "Stripe documentation", "consequence": "May affect the Customer Refund workflow",
             "available_actions": ["Review"], "priority": 60, **_prov("discovery", "finding:918")},
        ]

    def discovery(self, _pid: str) -> List[dict]:
        return [
            {"id": "918", "title": "Stripe refund API behavior changed", "detail": "Source: Stripe documentation",
             "confidence": "High", "needsReview": True,
             "affects": {"workflows": ["Customer Refund Handling"], "missions": ["4821"], "scheduled": 4},
             **_prov("discovery", "finding:918")},
            {"id": "919", "title": "HubSpot property schema changed", "detail": "Affects CRM reconciliation",
             "confidence": "Medium", "needsReview": False,
             "affects": {"workflows": ["CRM reconciliation"], "missions": [], "scheduled": 0}, **_prov("discovery")},
            {"id": "920", "title": "Contradictory evidence detected", "detail": "Mission: Portfolio Review #221",
             "confidence": "", "needsReview": False,
             "affects": {"workflows": [], "missions": ["221"], "scheduled": 0}, **_prov("discovery")},
        ]

    def activity(self, _pid: str) -> List[dict]:
        rows = [
            ("e1", "18:42", "Mission #4821 requested approval in Slack", "governance"),
            ("e2", "18:40", "Stripe charge ch_3Q… matched · $129", "connector"),
            ("e3", "18:39", "Discovery linked customer evidence · Sarah Chen", "discovery"),
            ("e4", "18:38", "WhatsApp request received · \"I was charged twice\"", "mission"),
            ("e5", "18:30", "Slack connection verified", "connector"),
            ("e6", "18:24", "Workflow policy updated · Customer Refund Handling", "project"),
        ]
        return [{"id": i, "time": t, "text": x, **_prov(rt)} for i, t, x, rt in rows]

    def apps(self, _pid: str) -> List[dict]:
        return _sample_apps()

    def overview(self, project_id: str) -> dict:
        return {
            "project": self.projects()[0],
            "attention": self.attention(project_id),
            "missions": self.missions(project_id),
            "workflows": self.workflows(project_id),
            "discovery": self.discovery(project_id),
            "apps": self.apps(project_id),
        }


def _app(provider: str, display_name: str, used_for: str, state: str, health: str,
         caps: List[str], **extra: Any) -> Dict[str, Any]:
    return {"provider": provider, "display_name": display_name, "used_for": used_for,
            "state": state, "health": health, "verified": extra.get("verified", ""),
            "capabilities": caps, "setup_url": extra.get("setup_url", ""),
            "manual_steps": extra.get("manual_steps", []), "required_scopes": extra.get("required_scopes", []),
            "credential_fields": extra.get("credential_fields", []), **_prov("connector", f"app:{provider}")}


def _sample_apps() -> List[dict]:
    return [
        _app("whatsapp_business", "WhatsApp Business", "Receive customer messages and reply.",
             "MISSION_READY", "warn", ["chat.message.send"], verified="Verified 2 days ago"),
        _app("hubspot", "HubSpot", "Find the customer and record the conversation.",
             "MISSION_READY", "ok", ["crm.contact.upsert", "crm.note.create"], verified="Verified 11 min ago"),
        _app("slack", "Slack", "Notifications, approvals, replies in threads.",
             "MISSION_READY", "ok", ["chat.message.send", "approval.request", "chat.message.read"], verified="Verified 3 min ago"),
        _app("stripe", "Stripe", "Find charges and execute approved refunds.",
             "MISSION_READY", "ok", ["billing.charge.find", "billing.refund.execute"], verified="Verified 5 min ago"),
        _app("gmail", "Gmail", "Send email confirmations (optional).",
             "NOT_CONNECTED", "mut", ["email.message.send"]),
        _app("ayrshare", "Ayrshare", "Publish content to many venues in one call.",
             "NOT_CONNECTED", "mut", ["content.publish"]),
    ]


def apps_from_setup_guides() -> Optional[List[dict]]:
    """Build the apps projection from the live connector setup guides when the ``[connectors]``
    plugin is installed — so the UI shows the real Integration Plane's providers, capabilities
    and setup steps. Returns ``None`` when the plugin is absent (caller uses the sample)."""
    try:
        from redevops_connectors import SETUP_GUIDES  # type: ignore
    except ImportError:
        return None
    out: List[dict] = []
    for g in SETUP_GUIDES.values():
        out.append({
            "provider": g.provider, "display_name": g.display_name, "used_for": g.used_for,
            "state": "NOT_CONNECTED", "health": "mut", "verified": "",
            "capabilities": [], "setup_url": g.setup_url,
            "manual_steps": list(g.manual_steps), "required_scopes": list(g.required_scopes),
            "credential_fields": [{"name": c.name, "label": c.label, "secret": c.secret}
                                  for c in g.credential_fields],
            **_prov("connector", f"app:{g.provider}", f"guide:{g.provider}"),
        })
    return out


def sidekick_reply(ctx: Dict[str, Any], text: str) -> Dict[str, Any]:
    """A governed conversational stand-in honouring the context contract — 'this' resolves to
    ``ctx.objectRef``. The real Sidekick compiles requests through the wizard + Mission Runtime;
    this keeps the surface live meanwhile."""
    t = (text or "").lower()
    obj = ctx.get("objectRef") or "this"
    if "two approver" in t or "$500" in t:
        return {"text": f"Proposed on {obj}: refunds above $500 require two approvers. Governed policy change — confirm to commit.",
                "actions": [{"label": "Confirm", "kind": "commit"}]}
    if "why" in t and "approv" in t:
        return {"text": "This is billing.refund.execute — a tier-4 action that moves money, so policy requires your approval. It runs under a GovernedEnvelope and is verified by re-reading the refund."}
    if "affected" in t or "which active mission" in t:
        return {"text": "The Stripe refund-API finding affects 1 workflow, 1 active Mission (#4821), and 4 scheduled Missions.",
                "actions": [{"label": "Pause scheduled", "kind": "pause"}]}
    if ("refund" in t and "whatsapp" in t) or "connect whatever" in t or "handle refund" in t:
        return {"text": "I'd wire WhatsApp → HubSpot → Stripe → Slack approval → refund → verify → reply. Slack/HubSpot/Stripe are connected; still needed: WhatsApp. Refund needs approval; Stripe test mode first.",
                "actions": [{"label": "Set this up", "kind": "setup"}, {"label": "Change something", "kind": "edit"}]}
    return {"text": "I can turn that into a governed Mission across your connected apps. Want me to propose the steps?"}


class _SidekickReq(BaseModel):
    ctx: Dict[str, Any] = {}
    text: str = ""


def create_app(provider: Optional[ProjectionProvider] = None, *, allow_origins: Optional[List[str]] = None) -> FastAPI:
    prov: ProjectionProvider = provider or SampleProjectionProvider()
    app = FastAPI(title="ReDevOps Projects API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware, allow_origins=allow_origins or ["*"],
        allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/projects")
    def _projects() -> List[dict]:
        return prov.projects()

    @app.get("/api/projects/{project_id}/overview")
    def _overview(project_id: str) -> dict:
        return prov.overview(project_id)

    @app.get("/api/projects/{project_id}/missions")
    def _missions(project_id: str) -> List[dict]:
        return prov.missions(project_id)

    @app.get("/api/projects/{project_id}/workflows")
    def _workflows(project_id: str) -> List[dict]:
        return prov.workflows(project_id)

    @app.get("/api/projects/{project_id}/attention")
    def _attention(project_id: str) -> List[dict]:
        return prov.attention(project_id)

    @app.get("/api/projects/{project_id}/discovery")
    def _discovery(project_id: str) -> List[dict]:
        return prov.discovery(project_id)

    @app.get("/api/projects/{project_id}/apps")
    def _apps(project_id: str) -> List[dict]:
        # Prefer the live Integration Plane guides when the connector plugin is installed.
        return apps_from_setup_guides() or prov.apps(project_id)

    @app.get("/api/projects/{project_id}/activity")
    def _activity(project_id: str) -> List[dict]:
        return prov.activity(project_id)

    @app.post("/api/sidekick")
    def _sidekick(req: _SidekickReq) -> dict:
        return sidekick_reply(req.ctx, req.text)

    return app


# Module-level app for `uvicorn agentic_os.projects_api:app`.
app = create_app()
