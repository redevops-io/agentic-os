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

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Set

try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
except ImportError as e:  # pragma: no cover
    raise ImportError("the Projects API needs fastapi (a base dependency)") from e


def _prov(runtime: str, *refs: str) -> Dict[str, Any]:
    return {"source_runtime": runtime, "source_refs": list(refs)}


# ── hosted OAuth (click-only Connect for the served deployment) ──────────────────
_HOSTED: Any = None
_HOSTED_TRIED = False


def _get_hosted() -> Any:
    """The deployment's :class:`HostedConnect`, built once from env. Returns None when the
    connector plugin isn't installed or no OAuth app's client creds are set (Connect then falls
    back to the simulated path)."""
    global _HOSTED, _HOSTED_TRIED
    if _HOSTED_TRIED:
        return _HOSTED
    _HOSTED_TRIED = True
    import os
    base = os.environ.get("PROJECTS_BASE_URL", "http://127.0.0.1:8787")
    try:
        from .integrations.hosted_oauth import HostedConnect, oauth_apps_from_env
        if oauth_apps_from_env(base):           # any provider's client creds present?
            _HOSTED = HostedConnect.from_env(base)
    except Exception:
        _HOSTED = None
    return _HOSTED


def _callback_page(body: str) -> str:
    return ("<!doctype html><meta charset=utf-8><title>ReDevOps Connect</title>"
            "<body style='font:16px system-ui;margin:4rem;text-align:center'>" + body + "</body>")


# ── the projection contract ─────────────────────────────────────────────────────
class ProjectionProvider(Protocol):
    def projects(self) -> List[dict]: ...
    def overview(self, project_id: str) -> dict: ...
    def missions(self, project_id: str) -> List[dict]: ...
    def mission_detail(self, project_id: str, mission_id: str) -> dict: ...
    def workflows(self, project_id: str) -> List[dict]: ...
    def attention(self, project_id: str) -> List[dict]: ...
    def discovery(self, project_id: str) -> List[dict]: ...
    def apps(self, project_id: str) -> List[dict]: ...
    def connect_app(self, provider: str) -> dict: ...
    def sources(self, project_id: str) -> List[dict]: ...
    def runtime(self, project_id: str) -> dict: ...
    def templates(self, project_id: str) -> List[dict]: ...
    def activity(self, project_id: str) -> List[dict]: ...


@dataclass
class SampleProjectionProvider:
    """Deterministic projections matching the UI's shapes — the API runs end-to-end with no
    Runtime wiring. Replace method-by-method with real projections; each already carries the
    provenance the UI drill-down expects."""

    project_id: str = "customer-ops"
    project_name: str = "Customer Operations"
    #: providers currently connected — mutated by connect_app so apps() and template
    #: readiness reflect real connection state (seeded from the sample's connected apps).
    connected: Set[str] = field(
        default_factory=lambda: {a["provider"] for a in _sample_apps() if a["state"] != "NOT_CONNECTED"} | {"polar", "apollo"})

    def projects(self) -> List[dict]:
        return [{"id": self.project_id, "name": self.project_name, "health": "ok"}]

    def missions(self, _pid: str) -> List[dict]:
        m = [
            ("4821", "Refund Sarah Chen", "Customer Refunds", "needs", "4/7", "mission:4821",
             ["HubSpot customer record", "Support Postgres", "WhatsApp conversation", "Refund policy PDF"]),
            ("triage", "Daily support triage", "Support Triage", "running", "84%", "mission:triage", []),
            ("vti", "Review VTI exposure", "Portfolio Review", "completed", "Verified", "mission:vti", []),
            ("rel24", "Deploy release 2.4", "Release Workflow", "failed", "Verify step", "mission:rel24", []),
            ("prospect", "Weekly prospecting", "Prospecting", "scheduled", "—", "mission:prospect", []),
            ("outreach", "Outreach — Tasha at Nutrients.tech", "Cold Outreach", "needs", "5/8", "mission:outreach",
             ["Generated copy", "Generated hero asset"]),
        ]
        return [{"id": i, "title": t, "workflow": w, "state": s, "progress": p,
                 "context_used": ctx, **_prov("mission", r)}
                for i, t, w, s, p, r, ctx in m]

    def mission_detail(self, project_id: str, mission_id: str) -> dict:
        summary = next((m for m in self.missions(project_id) if m["id"] == mission_id), None)
        if summary is None:
            return {}
        return {"summary": summary, **_mission_evidence(mission_id)}

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
        apps = _sample_apps()
        for a in apps:  # reflect the live connection set
            if a["provider"] in self.connected:
                if a["state"] == "NOT_CONNECTED":
                    a.update(state="VERIFIED_READ", health="ok", verified="Verified just now")
            else:
                a.update(state="NOT_CONNECTED", health="mut")
        return apps

    def connect_app(self, provider: str) -> dict:
        """Mark a provider connected (a served-app deployment plugs a real HostedConnectSession
        here; the sample simulates the outcome so Connect → readiness updates end to end)."""
        self.connected.add(provider)
        return {"provider": provider, "state": "VERIFIED_READ", "connected": True,
                "detail": "connected (sample: simulated hosted OAuth)"}

    def sources(self, _pid: str) -> List[dict]:
        return _sample_sources()

    def runtime(self, _pid: str) -> dict:
        return _sample_runtime()

    def templates(self, _pid: str) -> List[dict]:
        source_ids = {s["source_id"] for s in _sample_sources()}
        out: List[dict] = []
        for t in _sample_templates():
            deps = t.pop("_deps")
            t["readiness"] = [
                {"label": lbl, "ready": (key in self.connected) if kind == "app" else (key in source_ids)}
                for (lbl, kind, key) in deps
            ]
            out.append(t)
        return out

    def overview(self, project_id: str) -> dict:
        return {
            "project": self.projects()[0],
            "attention": self.attention(project_id),
            "missions": self.missions(project_id),
            "workflows": self.workflows(project_id),
            "discovery": self.discovery(project_id),
            "apps": self.apps(project_id),
            "sources": self.sources(project_id),
            "runtime": self.runtime(project_id),
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


def _source(source_id: str, name: str, kind: str, location: str, health_state: str, detail: str,
            stats: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    return {"source_id": source_id, "name": name, "kind": kind, "provider": extra.get("provider", ""),
            "location": location, "access_mode": extra.get("access_mode", "read_only"),
            "indexing_policy": extra.get("indexing_policy", "automatic"),
            "refresh_policy": extra.get("refresh_policy", "on_change"),
            "exposure_class": extra.get("exposure_class", "internal"),
            "health": {"state": health_state, "detail": detail, "last_observed_at": extra.get("last", "")},
            "stats": stats, "allowed_paths": extra.get("allowed_paths", []),
            "allowed_schemas": extra.get("allowed_schemas", []), "allowed_tables": extra.get("allowed_tables", []),
            "allowed_content_types": extra.get("allowed_content_types", []), "denied": extra.get("denied", []),
            "last_verified": extra.get("last", ""), "source_fingerprint": extra.get("fp", ""),
            **_prov("context", f"source:{source_id}")}


def _sample_sources() -> List[dict]:
    return [
        _source("pdfs", "Customer policy docs", "files", "~/company-docs", "healthy",
                "391 indexed", {"discovered": 428, "indexed": 391, "skipped": 37},
                allowed_content_types=["pdf", "docx", "markdown"], last="2 min ago", fp="a1b2c3"),
        _source("crm", "CRM database", "database", "postgres · localhost/customer_ops", "healthy",
                "Live connection", {"schemas": 2, "tables": 42}, provider="postgres",
                allowed_schemas=["public", "support"], denied=["billing.card_data", "hr.*"], last="live"),
        _source("gdrive", "Google Drive", "cloud_files", "Drive · Policies", "healthy",
                "Synced 4 min ago", {"files": 1842, "indexed": 1842}, provider="google_drive", last="4 min ago"),
        _source("s3", "Support archive", "cloud_files", "s3://support-archive", "stale",
                "Credentials expired · last sync 2 days ago", {"files": 12045}, provider="s3", last="2 days ago"),
    ]


def _sample_runtime() -> dict:
    h = lambda name, state, detail: {"name": name, "state": state, "detail": detail}
    return {
        "runtimes": [h("Mission Runtime", "ok", "Healthy"), h("Context Runtime", "ok", "Healthy"),
                     h("Discovery Runtime", "ok", "Healthy")],
        "models": [{"name": "Local model", "role": "primary", "state": "ok"},
                   {"name": "Cloud fallback", "role": "fallback", "state": "mut"}],
        "security": {"credential_broker": "ok", "policy": "ok"},
        "apps": [{"name": "WhatsApp", "state": "warn"}, {"name": "HubSpot", "state": "ok"},
                 {"name": "Slack", "state": "ok"}, {"name": "Polar", "state": "ok"}],
        "sources": [{"name": "Customer PDFs", "state": "ok"}, {"name": "CRM Postgres", "state": "ok"},
                    {"name": "Google Drive", "state": "ok"}, {"name": "S3 archive", "state": "warn"}],
        **_prov("project", "runtime:health"),
    }


def _sample_templates() -> List[dict]:
    # deps are (label, kind, key): kind "app" keys a provider id, "source" keys a source id.
    # readiness is computed against the live connection state in SampleProjectionProvider.templates.
    A, S = "app", "source"

    def tpl(tid, goal, caps, srcs, auth, wf, deps):
        return {"id": tid, "goal": goal, "required_capabilities": caps, "required_sources": srcs,
                "authority_requirements": auth, "suggested_workflow": wf, "_deps": deps}
    return [
        tpl("refunds", "Handle customer refunds",
            ["chat.message.send", "crm.contact.upsert", "approval.request", "billing.refund.execute"],
            ["Customer policy docs", "CRM database"], ["Approval required before refund"],
            "Customer Refund Handling",
            [("WhatsApp", A, "whatsapp_business"), ("HubSpot", A, "hubspot"), ("Slack", A, "slack"),
             ("Polar", A, "polar"), ("Customer policy docs", S, "pdfs"), ("CRM database", S, "crm")]),
        tpl("prospect", "Run weekly prospecting",
            ["crm.contact.upsert", "email.message.send"], ["CRM database"], [],
            "Weekly Prospecting",
            [("Apollo", A, "apollo"), ("Gmail", A, "gmail"), ("HubSpot", A, "hubspot"), ("CRM database", S, "crm")]),
        tpl("triage", "Review support queue", ["chat.message.read", "crm.contact.upsert"],
            ["CRM database"], [], "Daily Support Triage",
            [("Slack", A, "slack"), ("HubSpot", A, "hubspot"), ("CRM database", S, "crm")]),
        tpl("reconcile", "Reconcile CRM", ["crm.contact.upsert", "crm.note.create"], ["CRM database"], [],
            "CRM Reconciliation", [("HubSpot", A, "hubspot"), ("CRM database", S, "crm")]),
        tpl("outreach", "Run cold outreach for the agentic-apps stack",
            ["generate.copy", "generate.asset", "outreach.sequence.configure", "outreach.enroll"],
            [], ["Sequence activation is provider-UI-only (a human toggles it on)"],
            "Cold Outreach",
            [("Apollo", A, "apollo")]),
    ]


def _mission_evidence(mission_id: str) -> Dict[str, Any]:
    """The Mission's ACTIONS-used and EVIDENCE-used, each with a 'why' — the Used+Why half of
    the Available/Used/Why symmetry (Available comes from /apps and /sources). Evidence
    distinguishes QUERY evidence (records retrieved live, with count + observed time) from
    CATALOG/FILE evidence (identity: fingerprint/version), matching sources_postgres."""
    if mission_id == "outreach":
        return _outreach_evidence()
    if mission_id != "4821":
        return {"steps": [], "context_used": [], "context_plan_note": ""}
    steps = [  # ACTIONS used · provider chosen · why (EXPLAIN)
        {"n": 1, "capability": "chat.message.read", "provider": "whatsapp_business", "tier": 2,
         "status": "done", "why": "inbound channel the request arrived on"},
        {"n": 2, "capability": "crm.contact.upsert", "provider": "hubspot", "tier": 2,
         "status": "done", "why": "named CRM; only connected contact store"},
        {"n": 3, "capability": "billing.order.find", "provider": "polar", "tier": 1,
         "status": "done", "why": "billing provider of record for this account"},
        {"n": 4, "capability": "approval.request", "provider": "slack", "tier": 3,
         "status": "waiting", "why": "policy requires human approval before a refund"},
        {"n": 5, "capability": "billing.refund.execute", "provider": "polar", "tier": 4,
         "status": "todo", "why": "moves money — runs only after approval, then verified"},
    ]
    context_used = [  # EVIDENCE used · how retrieved · why (Context Plan)
        {"source_id": "crm", "source_name": "CRM database", "provider": "postgres", "kind": "database",
         "evidence_kind": "query",
         "retrieved": {"count": 3, "observed_at": "2026-09-09T11:31:00Z"}, "identity": None,
         "refs": [{"ref": "postgres:customers#0", "summary": "id=8821 · email=sarah@…"},
                  {"ref": "postgres:support.tickets#0", "summary": "subject=Billed twice · status=open"}],
         "why": "scoped SQL against the live source — Context Runtime queried in place rather than ingesting the DB"},
        {"source_id": "pdfs", "source_name": "Refund Policies", "provider": "google_drive", "kind": "cloud_files",
         "evidence_kind": "file",
         "retrieved": None, "identity": {"fingerprint": "a1b2c3", "version": "v19"},
         "refs": [{"ref": "gdrive:f1", "summary": "Refund Policy.pdf"}],
         "why": "vector retrieval over indexed policy PDFs — the right representation for prose"},
    ]
    return {"steps": steps, "context_used": context_used,
            "context_plan_note": "Sources define what evidence is available; Context Runtime chose SQL-in-place "
                                 "for the structured customer data and vector retrieval for the policy prose."}


def _outreach_evidence() -> Dict[str, Any]:
    """The outreach Mission (synthesis → configure → boundary → verify). Logical steps are
    portable; the ACTIVATE_SEQUENCE step is a PHYSICAL capability result — Apollo activation is
    provider-UI-only, so it waits on a human. Generated copy + hero image are the created
    artifacts (creative asset is optional)."""
    steps = [
        {"n": 1, "capability": "prepare.outreach", "provider": "runtime", "tier": 0,
         "status": "done", "why": "target Tasha · Nutrients.tech · cold outreach"},
        {"n": 2, "capability": "generate.copy", "provider": "claude", "tier": 0,
         "status": "done", "why": "grounded in a nutrition-tech ops example; subject prefixed [test]"},
        {"n": 3, "capability": "generate.asset", "provider": "fal.ai", "tier": 0,
         "status": "done", "why": "optional creative asset — multimodal composition"},
        {"n": 4, "capability": "outreach.sequence.configure", "provider": "apollo", "tier": 3,
         "status": "done", "why": "built the sequence step + email template (wait_mode day; template endpoint)"},
        {"n": 5, "capability": "outreach.enroll", "provider": "apollo", "tier": 3,
         "status": "done", "why": "enrolled tasha@nutrients.tech from the warmed mailbox"},
        {"n": 6, "capability": "outreach.sequence.activate", "provider": "apollo", "tier": 4,
         "status": "waiting", "why": "PROVIDER_UI_REQUIRED — Apollo activation is UI-only (capability advertises "
                                     "automatable=false); a human flips the sequence on"},
        {"n": 7, "capability": "outreach.observe", "provider": "apollo", "tier": 1,
         "status": "todo", "why": "after activation, observe the send through the mailbox"},
        {"n": 8, "capability": "verify.delivery", "provider": "apollo", "tier": 1,
         "status": "todo", "why": "confirm delivery and produce an ExecutionReceipt"},
    ]
    context_used = [
        {"source_id": "copy", "source_name": "Generated copy", "provider": "claude", "kind": "artifact",
         "evidence_kind": "file", "retrieved": None, "identity": {"version": "v1"},
         "refs": [{"ref": "artifact:copy", "summary": "[test] One governed system for Nutrients.tech's apps + data"}],
         "why": "synthesized from the goal + target context"},
        {"source_id": "asset", "source_name": "Generated hero asset", "provider": "fal.ai", "kind": "artifact",
         "evidence_kind": "file", "retrieved": None, "identity": {"version": "v1"},
         "refs": [{"ref": "artifact:hero", "summary": "conceptual hero — apps + DB + doc → one governed system"}],
         "preview": "/hero.jpg", "why": "optional creative asset (copy is required; asset is not)"},
    ]
    return {"steps": steps, "context_used": context_used,
            "context_plan_note": "Logical outreach workflow is provider-independent; activation is a physical "
                                 "capability result — Apollo is provider-UI-only, so the Mission pauses for a human."}


def propose_sources(project_id: str, text: str):
    """Deterministic 'use X as context' interpreter → an editable SourceConnectionProposal.
    Mirrors the integration wizard: infers reversible choices (read-only, content types),
    asks only what can't be safely guessed (which tables). The real Sidekick uses an LLM read."""
    from .sources import AccessMode, IndexingPolicy, ProposedSource, SourceConnectionProposal, SourceKind
    import re

    t = (text or "")
    tl = t.lower()
    proposed: List[Any] = []
    assumptions: List[str] = []
    questions: List[str] = []

    for m in re.finditer(r"(~?/[\w./\-]+)", t):  # any unix-ish path
        proposed.append(ProposedSource(kind=SourceKind.FILES, location=m.group(1),
                                       access_mode=AccessMode.READ_ONLY, indexing_policy=IndexingPolicy.AUTOMATIC))
        assumptions.append(f"{m.group(1)} — read-only, automatic indexing, common document types")
    if "postgres" in tl or "database" in tl or "db " in tl:
        loc = "localhost/customer_ops"
        mm = re.search(r"database on ([\w.:/\-]+)", tl)
        if mm:
            loc = mm.group(1)
        proposed.append(ProposedSource(kind=SourceKind.DATABASE, location=loc, provider="postgres",
                                       access_mode=AccessMode.READ_ONLY))
        assumptions.append(f"{loc} — read-only connection")
        questions.append("Which schemas/tables may be used as context?")
    if "google drive" in tl or "gdrive" in tl:
        proposed.append(ProposedSource(kind=SourceKind.CLOUD_FILES, location="Google Drive",
                                       provider="google_drive", access_mode=AccessMode.READ_ONLY))
        assumptions.append("Google Drive — connect via provider OAuth, then pick folders")

    return SourceConnectionProposal(project_id=project_id, sources=proposed,
                                    assumptions=assumptions, questions=questions)


def build_source_registry(resolver: Optional[Any] = None, *, use_rag: Optional[bool] = None) -> Any:
    """A registry routing each source kind to its real connector. Files are always real
    (stdlib LocalFilesConnector). Database (PostgreSQL) and cloud files (Google Drive) are
    registered — and connect for real — only when a credential ``resolver`` is provided (a
    deployment binds its CredentialBroker here). Without one they stay 'pending' rather than
    attempting a live connection during a demo.

    ``use_rag`` (or ``$PROJECTS_RAG``) binds the live Context-Runtime/RAG indexer to the files
    connector so file/drive content is actually indexed + retrievable — needs ``[rag]``."""
    import os
    from .sources import LocalFilesConnector, SourceConnectorRegistry
    if use_rag is None:
        use_rag = os.environ.get("PROJECTS_RAG", "").lower() in ("1", "true", "yes")

    reg = SourceConnectorRegistry()
    if use_rag:
        from .sources_rag import RagIndexer
        reg.register(LocalFilesConnector(indexer=RagIndexer()))  # real indexing + retrieval
    else:
        reg.register(LocalFilesConnector())  # stdlib scan (CountingIndexer)
    if resolver is not None:
        from .sources_drive import GoogleDriveSourceConnector
        from .sources_postgres import PostgresSourceConnector
        indexer_kw = {}
        if use_rag:
            from .sources_rag import RagIndexer
            indexer_kw = {"indexer": RagIndexer()}
        reg.register(PostgresSourceConnector(resolver=resolver))
        reg.register(GoogleDriveSourceConnector(resolver=resolver, **indexer_kw))
    return reg


def confirm_and_connect_sources(project_id: str, source_specs: List[dict], confirmed_by: str,
                                registry: Optional[Any] = None) -> List[dict]:
    """Confirm an (already-answered) proposal and actually connect the sources through the
    right connector. Files are scanned for real by the stdlib LocalFilesConnector; database/
    cloud route to PostgresSourceConnector/GoogleDriveSourceConnector when the ``registry``
    carries them (see :func:`build_source_registry`), else report 'pending'. Returns
    ContextSource projections for the UI."""
    from .sources import (AccessMode, ConfirmedSourceIntent, IndexingPolicy, ProposedSource,
                          SourceConnectorRegistry, SourceKind)

    specs = []
    for s in source_specs:
        specs.append(ProposedSource(
            kind=SourceKind(s.get("kind", "files")), location=s.get("location", ""),
            name=s.get("name", ""), provider=s.get("provider", ""),
            access_mode=AccessMode(s.get("access_mode", "read_only")),
            allowed_content_types=list(s.get("allowed_content_types", [])),
            allowed_schemas=list(s.get("allowed_schemas", [])),
            indexing_policy=IndexingPolicy(s.get("indexing_policy", "automatic")),
        ))
    intent = ConfirmedSourceIntent(project_id=project_id, sources=tuple(specs),
                                   confirmed_by=confirmed_by, confirmed_at="")
    reg = registry if registry is not None else SourceConnectorRegistry.default()
    return [cs.to_projection() for cs in reg.connect(intent)]


def apps_from_setup_guides(connected: Optional[Set[str]] = None) -> Optional[List[dict]]:
    """Build the apps projection from the live connector setup guides when the ``[connectors]``
    plugin is installed — so the UI shows the real Integration Plane's providers, capabilities
    and setup steps. Connection *state* is not the guide's to know: it comes from ``connected``
    (the provider's live set), so Connect flips a provider here too. Returns ``None`` when the
    plugin is absent (caller uses the sample)."""
    try:
        from redevops_connectors import SETUP_GUIDES  # type: ignore
    except ImportError:
        return None
    connected = connected or set()
    out: List[dict] = []
    for g in SETUP_GUIDES.values():
        is_on = g.provider in connected
        out.append({
            "provider": g.provider, "display_name": g.display_name, "used_for": g.used_for,
            "state": "VERIFIED_READ" if is_on else "NOT_CONNECTED",
            "health": "ok" if is_on else "mut", "verified": "Verified just now" if is_on else "",
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
    # Sidekick is also the stack's in-product expert: authoritative, curated answers about how
    # credentials are handled, where data is processed (local vs cloud), and how execution is
    # governed — so nobody has to read a manual. Consulted before the generic fallback.
    from agentic_os.stack_knowledge import answer_stack_question
    kb = answer_stack_question(text)
    if kb is not None:
        return {"text": kb.answer, "topic": kb.topic,
                "actions": [{"label": "Where this is enforced", "kind": "explain", "ref": kb.source}]}
    return {"text": "I can turn that into a governed Mission across your connected apps. Want me to propose the steps? "
                    "You can also ask me how your credentials are handled or whether your data stays local."}


class _SidekickReq(BaseModel):
    ctx: Dict[str, Any] = {}
    text: str = ""


class _ProposeSourcesReq(BaseModel):
    project_id: str = "customer-ops"
    text: str = ""


class _ConfirmSourcesReq(BaseModel):
    project_id: str = "customer-ops"
    sources: List[Dict[str, Any]] = []
    confirmed_by: str = ""


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

    @app.get("/api/projects/{project_id}/missions/{mission_id}")
    def _mission_detail(project_id: str, mission_id: str) -> dict:
        return prov.mission_detail(project_id, mission_id)

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
        # Prefer the live Integration Plane guides when the connector plugin is installed,
        # overlaying the provider's live connection set; else the sample provider (also stateful).
        guided = apps_from_setup_guides(getattr(prov, "connected", None))
        return guided if guided is not None else prov.apps(project_id)

    @app.post("/api/apps/{provider}/connect")
    def _connect_app(provider: str) -> dict:
        # Simulated connect (no hosted OAuth app configured) — flips readiness for the demo.
        return prov.connect_app(provider)

    @app.post("/api/apps/{provider}/connect/start")
    def _connect_start(provider: str) -> dict:
        """Begin hosted OAuth: return the provider consent URL the browser should open. Falls
        back to the simulated connect when no ReDevOps OAuth app is registered for the provider."""
        hosted = _get_hosted()
        if hosted is not None and hosted.available(provider):
            started = hosted.start(provider)
            return {"hosted": True, "authorize_url": started["authorize_url"], "state": started["state"]}
        return {"hosted": False, "authorize_url": None, "connected": prov.connect_app(provider)}

    @app.get("/api/apps/connect/callback")
    def _connect_callback(code: str = "", state: str = "") -> "HTMLResponse":
        """The hosted redirect target: exchange the code, store the token, verify, and reflect
        the connection in the projections. Returns a small page the user closes."""
        from fastapi.responses import HTMLResponse
        hosted = _get_hosted()
        if hosted is None:
            return HTMLResponse("<p>Hosted OAuth is not configured on this deployment.</p>", status_code=400)
        outcome = hosted.callback(code, state)
        if outcome.connected:
            prov.connect_app(outcome.provider)  # reflect in /apps + template readiness
            body = (f"<h2>&#10003; Connected {outcome.provider}</h2>"
                    f"<p>{outcome.state} · you can close this tab and return to Projects.</p>")
            return HTMLResponse(_callback_page(body))
        return HTMLResponse(_callback_page(f"<h2>Couldn't connect</h2><p>{outcome.detail}</p>"), status_code=400)

    @app.get("/api/projects/{project_id}/sources")
    def _sources(project_id: str) -> List[dict]:
        return prov.sources(project_id)

    @app.get("/api/projects/{project_id}/runtime")
    def _runtime(project_id: str) -> dict:
        return prov.runtime(project_id)

    @app.get("/api/projects/{project_id}/templates")
    def _templates(project_id: str) -> List[dict]:
        return prov.templates(project_id)

    @app.get("/api/projects/{project_id}/activity")
    def _activity(project_id: str) -> List[dict]:
        return prov.activity(project_id)

    @app.post("/api/sidekick")
    def _sidekick(req: _SidekickReq) -> dict:
        return sidekick_reply(req.ctx, req.text)

    @app.get("/api/sidekick/help")
    def _sidekick_help() -> List[dict]:
        # The canonical stack questions Sidekick can answer authoritatively — for a "what can I
        # ask" surface (how creds are handled, is my data local/cloud, what can a connected app see…).
        from agentic_os.stack_knowledge import help_questions
        return list(help_questions())

    @app.post("/api/sources/propose")
    def _propose(req: _ProposeSourcesReq) -> dict:
        return propose_sources(req.project_id, req.text).to_dict()

    @app.post("/api/sources/confirm")
    def _confirm(req: _ConfirmSourcesReq) -> List[dict]:
        return confirm_and_connect_sources(req.project_id, req.sources, req.confirmed_by)

    # Serve the bundled Projects UI at the SAME origin as the API (no CORS) — mounted LAST so
    # the /api routes above always win. The UI is built with VITE_PROJECTS_API="/", so it calls
    # this service's /api directly. `pip install 'agentic-os[projects]'` ships the bundle.
    ui = _ui_dir()
    if ui is not None:
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=str(ui), html=True), name="projects-ui")

    return app


def _ui_dir() -> Optional["Path"]:
    """The built Projects UI directory: ``$PROJECTS_UI_DIR`` if set, else the bundle vendored at
    ``agentic_os/projects_ui/``. Returns None when no build is present (API-only)."""
    import os
    from pathlib import Path
    candidates = []
    env = os.environ.get("PROJECTS_UI_DIR")
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).resolve().parent / "projects_ui")
    for c in candidates:
        if (c / "index.html").is_file():
            return c
    return None


def mounted_app(base_path: str = "", provider: Optional[ProjectionProvider] = None, **kw) -> FastAPI:
    """Build the app, optionally under a base path (e.g. ``/projects``) so it can be fronted at
    a URL sub-path — cloudflared routes ``demo.redevops.io/projects`` → this service without
    stripping, so the app serves ``/projects/api`` + ``/projects/`` (the UI must be built with a
    matching ``base``). Without ``base_path`` the app serves at the origin root."""
    inner = create_app(provider, **kw)
    base = (base_path or "").rstrip("/")
    if not base:
        return inner
    outer = FastAPI(title="ReDevOps Projects", version="0.1.0")
    outer.mount(base, inner)   # Starlette strips the prefix before the inner app sees it
    return outer


def main() -> None:
    """Console entrypoint (``agentic-os-projects``): serve the Projects UI + API on one origin,
    optionally under ``$PROJECTS_BASE_PATH``."""
    import os
    import uvicorn
    host = os.environ.get("PROJECTS_HOST", "127.0.0.1")
    port = int(os.environ.get("PROJECTS_PORT", "8787"))
    base = os.environ.get("PROJECTS_BASE_PATH", "")
    served = "UI + API" if _ui_dir() is not None else "API only (no UI bundle found)"
    print(f"ReDevOps Projects — {served} on http://{host}:{port}{base or ''}")
    uvicorn.run(mounted_app(base), host=host, port=port)


# Module-level app for `uvicorn agentic_os.projects_api:app` (honours $PROJECTS_BASE_PATH).
import os as _os
app = mounted_app(_os.environ.get("PROJECTS_BASE_PATH", ""))
