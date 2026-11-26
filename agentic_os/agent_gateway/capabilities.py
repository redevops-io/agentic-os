"""Phase 1 read capabilities (plan §13 Phase 1): read-only Projects, Missions, CRM, and Sources
exposed as *semantic* capabilities (plan §2), never raw CRUD.

Backends are read-port seams so this module stays decoupled from the Mission Runtime / projection
providers (and their heavy deps): :func:`build_read_registry` registers only the capabilities whose
backend is supplied, each behind a handler that calls the port and never bypasses the gateway.
"""
from __future__ import annotations

import re
from typing import Any, Callable, List, Optional, Protocol

from .contracts import ApprovalPolicy, CapabilityKind, CapabilityManifest, DataClass, GatewayPrincipal, RiskTier
from .registry import CapabilityRegistry, HandlerResult


# ── read-port seams (adapt the real backends to these in wiring) ──────────────────
class ProjectionsReadPort(Protocol):
    def projects(self) -> List[dict]: ...
    def overview(self, project_id: str) -> dict: ...


class MissionReadPort(Protocol):
    def list(self) -> List[dict]: ...
    def get(self, mission_id: str) -> dict: ...
    def explain(self, mission_id: str) -> dict: ...
    def pending_approvals(self) -> List[dict]: ...


class SourcesReadPort(Protocol):
    def search(self, query: str, limit: int = 10) -> List[dict]: ...


class DashboardsReadPort(Protocol):
    """The dashboards, read AS the calling principal: an implementation decides from it what may be counted."""
    def catalog(self, principal: GatewayPrincipal) -> dict: ...
    def get(self, principal: GatewayPrincipal, dashboard_id: str, days: int = 30, project: str = "") -> dict: ...


class CrmReadPort(Protocol):
    def lookup(self, query: str) -> dict: ...


def _obj_schema(**props: str) -> dict:
    """JSON-schema-lite for a flat object of string params (all required)."""
    return {"type": "object",
            "properties": {k: {"type": v} for k, v in props.items()},
            "required": list(props)}


_NO_INPUT = {"type": "object", "properties": {}}


# ── manifests (all read-only ⇒ RiskTier.READ, non-side-effecting, auto-approve) ────
PROJECTS_LIST = CapabilityManifest(
    "projects.list", "List the projects you can see.", CapabilityKind.DIRECT,
    permissions=("projects.read",), input_schema=_NO_INPUT, data_classes=(DataClass.INTERNAL,))
PROJECTS_GET_STATUS = CapabilityManifest(
    "projects.get_status", "Get a project's current status overview.", CapabilityKind.DIRECT,
    permissions=("projects.read",),             # no project_id: the deployment's default project
    input_schema={"type": "object", "properties": {"project_id": {"type": "string"}}},
    data_classes=(DataClass.INTERNAL,))
MISSIONS_LIST = CapabilityManifest(
    "missions.list", "List missions and their state.", CapabilityKind.DIRECT,
    permissions=("missions.read",), input_schema=_NO_INPUT, data_classes=(DataClass.INTERNAL,))
MISSIONS_GET = CapabilityManifest(
    "missions.get", "Get one mission's summary and state.", CapabilityKind.DIRECT,
    permissions=("missions.read",), input_schema=_obj_schema(mission_id="string"),
    data_classes=(DataClass.INTERNAL,))
MISSIONS_EXPLAIN = CapabilityManifest(
    "missions.explain", "Explain how a mission was planned and gated.", CapabilityKind.DIRECT,
    permissions=("missions.read",), input_schema=_obj_schema(mission_id="string"),
    data_classes=(DataClass.INTERNAL,))
MISSIONS_PENDING = CapabilityManifest(
    "missions.list_pending_approvals", "List mission steps awaiting human approval.",
    CapabilityKind.DIRECT, permissions=("missions.read",), input_schema=_NO_INPUT, data_classes=(DataClass.INTERNAL,))
SOURCES_SEARCH = CapabilityManifest(
    "sources.search", "Search connected knowledge sources; returns references, not copies.",
    CapabilityKind.DIRECT, permissions=("sources.read",),
    input_schema={"type": "object",
                  "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                  "required": ["query"]},
    data_classes=(DataClass.INTERNAL,))
DASHBOARDS_LIST = CapabilityManifest(
    "dashboards.list", "List the dashboards you can read here (for example QA and release readiness, portfolio health, "
    "missions) and those that are not available, with why.", CapabilityKind.DIRECT,
    permissions=("dashboards.read",), input_schema=_NO_INPUT, data_classes=(DataClass.INTERNAL,))
DASHBOARDS_GET = CapabilityManifest(
    "dashboards.get", "Read one dashboard: its summary, panels with their numbers, sources and how fresh each is. It returns "
    "figures and at most a few table rows, not the underlying records. E-mail addresses are masked; names of people and "
    "companies are not.", CapabilityKind.DIRECT,
    permissions=("dashboards.read",), data_classes=(DataClass.INTERNAL,),
    input_schema={"type": "object", "properties": {"dashboard_id": {"type": "string"}, "days": {"type": "integer"}, "project": {"type": "string"}},
                  "required": ["dashboard_id"]})
CRM_LOOKUP = CapabilityManifest(
    "crm.lookup_account", "Look up a CRM account/contact by name, email, or id.",
    CapabilityKind.DIRECT, permissions=("crm.read",), input_schema=_obj_schema(query="string"),
    data_classes=(DataClass.CUSTOMER_CONTENT, DataClass.PII))


#: A table is cut to this many rows for an agent, and says how many there are: a busy dashboard was 17,000 to 25,000
#: characters, which an agent with a small context cannot take whole, and one that cannot see the end of what it was given
#: asks again and may never answer.
AGENT_MAX_ROWS = 10
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def bound_rows(dash: dict, max_rows: int = AGENT_MAX_ROWS) -> dict:
    for panel in dash.get("panels") or []:
        rows = panel.get("rows")
        if isinstance(rows, list) and len(rows) > max_rows:
            panel["rows_total"], panel["rows_shown"] = len(rows), max_rows
            panel["rows_note"] = (f"{len(rows) - max_rows} more row(s) are not returned to an agent; counts elsewhere in this "
                                  f"dashboard are of all {len(rows)}. The records are in the app.")
            panel["rows"] = rows[:max_rows]
    return dash


def mask_emails(value: Any) -> Any:
    """E-mail addresses only. The general redaction would also mask commit and image digests, which a dashboard reports."""
    if isinstance(value, dict):
        return {k: mask_emails(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_emails(v) for v in value]
    return _EMAIL.sub("\u00abredacted\u00bb", value) if isinstance(value, str) else value


def _ok(output: Any, classes) -> HandlerResult:
    return HandlerResult(ok=True, output=output, data_classes=classes)


def build_read_registry(*, projections: Optional[ProjectionsReadPort] = None,
                        missions: Optional[MissionReadPort] = None,
                        sources: Optional[SourcesReadPort] = None,
                        crm: Optional[CrmReadPort] = None,
                        dashboards: Optional[DashboardsReadPort] = None,
                        scope: Optional[Callable[[GatewayPrincipal], Optional[Callable[[str], bool]]]] = None,
                        registry: Optional[CapabilityRegistry] = None) -> CapabilityRegistry:
    """Register the read capabilities whose backend is provided, onto a new or given registry.

    ``scope`` (a deployment with tenants, see ``scope.py``) says which projects the CALLER may see: ``None`` for a caller
    means unscoped. Mission, attention and source reads are not reliably separable by project in every provider, so a
    caller who may not see every project gets none of them, and for a project it may see, only that project's listing."""
    reg = registry or CapabilityRegistry()

    def _listed() -> dict:
        return {str(p.get("id")): p for p in projections.projects()} if projections is not None else {}

    def _visible(req) -> Optional[Callable[[str], bool]]:
        return scope(req.principal) if scope is not None else None

    def _all_projects(req) -> bool:
        visible = _visible(req)
        if visible is None:
            return True
        known = _listed()
        return bool(known) and all(visible(i) for i in known)

    def _deployment_wide(req, read: Callable[[], Any], empty: Any = None, missing: str = "") -> HandlerResult:
        if _all_projects(req):
            return _ok(read(), (DataClass.INTERNAL,))
        return HandlerResult(ok=False, error=missing) if missing else _ok(empty, (DataClass.INTERNAL,))

    def _projects(req, env) -> HandlerResult:
        visible, items = _visible(req), projections.projects()
        return _ok(items if visible is None else [p for p in items if visible(str(p.get("id")))], (DataClass.INTERNAL,))

    def _status(req, env) -> HandlerResult:
        asked, visible = str(req.arguments.get("project_id", "")), _visible(req)
        if visible is None:
            return _ok(projections.overview(asked), (DataClass.INTERNAL,))
        known = _listed()
        wanted = asked or next(iter(known), "")
        if wanted not in known or not visible(wanted):             # one answer for "no such" and "not yours"
            return HandlerResult(ok=False, error="no such project")
        return _ok(projections.overview(wanted) if _all_projects(req) else {"project": known[wanted]}, (DataClass.INTERNAL,))

    if projections is not None:
        reg.register(PROJECTS_LIST, _projects)
        reg.register(PROJECTS_GET_STATUS, _status)
    if missions is not None:
        reg.register(MISSIONS_LIST, lambda req, env: _deployment_wide(req, missions.list, empty=[]))
        reg.register(MISSIONS_GET, lambda req, env: _deployment_wide(
            req, lambda: missions.get(str(req.arguments.get("mission_id", ""))), missing="no such mission"))
        reg.register(MISSIONS_EXPLAIN, lambda req, env: _deployment_wide(
            req, lambda: missions.explain(str(req.arguments.get("mission_id", ""))), missing="no such mission"))
        reg.register(MISSIONS_PENDING, lambda req, env: _deployment_wide(req, missions.pending_approvals, empty=[]))
    if sources is not None:
        reg.register(SOURCES_SEARCH, lambda req, env: _deployment_wide(
            req, lambda: sources.search(str(req.arguments.get("query", "")), int(req.arguments.get("limit", 10))), empty=[]))
    if dashboards is not None:
        reg.register(DASHBOARDS_LIST, lambda req, env: _ok(mask_emails(dashboards.catalog(req.principal)), (DataClass.INTERNAL,)))
        reg.register(DASHBOARDS_GET, lambda req, env: _ok(mask_emails(bound_rows(dashboards.get(
            req.principal, str(req.arguments.get("dashboard_id", "")), int(req.arguments.get("days", 30) or 30),
            str(req.arguments.get("project", "") or "")))), (DataClass.INTERNAL,)))
    if crm is not None:
        reg.register(CRM_LOOKUP,
                     lambda req, env: _ok(crm.lookup(str(req.arguments.get("query", ""))),
                                          (DataClass.CUSTOMER_CONTENT, DataClass.PII)))
    return reg


READ_CAPABILITIES = (
    PROJECTS_LIST, PROJECTS_GET_STATUS, MISSIONS_LIST, MISSIONS_GET, MISSIONS_EXPLAIN,
    MISSIONS_PENDING, SOURCES_SEARCH, DASHBOARDS_LIST, DASHBOARDS_GET, CRM_LOOKUP)


# ── Phase 2: mission delegation + control (plan §4) ────────────────────────────────
#: The flagship. An external agent delegates a GOAL; the Mission Runtime plans and executes it and
#: gates its own side effects — so this itself is only a bounded write (starting a governed run).
MISSIONS_DELEGATE_GOAL = CapabilityManifest(
    "missions.delegate_goal",
    "Delegate a goal; ReDevOps plans and runs it as a governed mission and returns a mission id. "
    "Side effects inside the mission still require human approval.",
    CapabilityKind.MISSION, permissions=("missions.delegate",), risk_tier=RiskTier.BOUNDED_WRITE,
    side_effecting=True, approval_policy=ApprovalPolicy.IF_POLICY,
    input_schema={"type": "object",
                  "properties": {"goal": {"type": "string"},
                                 "constraints": {"type": "array", "items": {"type": "string"}}},
                  "required": ["goal"]},
    output_schema=_obj_schema(mission_id="string", state="string"))
MISSIONS_PAUSE = CapabilityManifest(
    "missions.pause", "Pause (suspend) a running mission.", CapabilityKind.DIRECT,
    permissions=("missions.control",), risk_tier=RiskTier.BOUNDED_WRITE, side_effecting=True,
    approval_policy=ApprovalPolicy.IF_POLICY, input_schema=_obj_schema(mission_id="string"))
MISSIONS_RESUME = CapabilityManifest(
    "missions.resume", "Resume a paused mission.", CapabilityKind.DIRECT,
    permissions=("missions.control",), risk_tier=RiskTier.BOUNDED_WRITE, side_effecting=True,
    approval_policy=ApprovalPolicy.IF_POLICY, input_schema=_obj_schema(mission_id="string"))


def register_mission_capabilities(registry: CapabilityRegistry, adapter, *,
                                  include_reads: bool = True) -> CapabilityRegistry:
    """Register the mission capabilities backed by a MissionRuntimeAdapter.

    NOTE: the caller must also wire the same adapter as the gateway's ``mission`` port (that is what
    fulfils the MISSION-kind ``missions.delegate_goal``); the reads + pause/resume are DIRECT
    handlers over the adapter. ``submit_approval`` is deliberately NOT here — approving a mission
    gate is a human control-plane action (Phase 5), not something the external agent self-serves.
    """
    if include_reads:
        build_read_registry(missions=adapter, registry=registry)
    registry.register(MISSIONS_DELEGATE_GOAL)      # MISSION kind → routed to gateway.mission
    registry.register(MISSIONS_PAUSE,
                      lambda req, env: _ok(adapter.pause(str(req.arguments.get("mission_id", "")),
                                                         actor=req.principal.subject),
                                           (DataClass.INTERNAL,)))
    registry.register(MISSIONS_RESUME,
                      lambda req, env: _ok(adapter.resume(str(req.arguments.get("mission_id", "")),
                                                          actor=req.principal.subject),
                                           (DataClass.INTERNAL,)))
    return registry
