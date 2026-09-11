"""Phase 1 read capabilities (plan §13 Phase 1): read-only Projects, Missions, CRM, and Sources
exposed as *semantic* capabilities (plan §2), never raw CRUD.

Backends are read-port seams so this module stays decoupled from the Mission Runtime / projection
providers (and their heavy deps): :func:`build_read_registry` registers only the capabilities whose
backend is supplied, each behind a handler that calls the port and never bypasses the gateway.
"""
from __future__ import annotations

from typing import Any, List, Optional, Protocol

from .contracts import ApprovalPolicy, CapabilityKind, CapabilityManifest, DataClass, RiskTier
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


class CrmReadPort(Protocol):
    def lookup(self, query: str) -> dict: ...


def _obj_schema(**props: str) -> dict:
    """JSON-schema-lite for a flat object of string params (all required)."""
    return {"type": "object",
            "properties": {k: {"type": v} for k, v in props.items()},
            "required": list(props)}


# ── manifests (all read-only ⇒ RiskTier.READ, non-side-effecting, auto-approve) ────
PROJECTS_LIST = CapabilityManifest(
    "projects.list", "List the projects you can see.", CapabilityKind.DIRECT,
    permissions=("projects.read",), data_classes=(DataClass.INTERNAL,))
PROJECTS_GET_STATUS = CapabilityManifest(
    "projects.get_status", "Get a project's current status overview.", CapabilityKind.DIRECT,
    permissions=("projects.read",), input_schema=_obj_schema(project_id="string"),
    data_classes=(DataClass.INTERNAL,))
MISSIONS_LIST = CapabilityManifest(
    "missions.list", "List missions and their state.", CapabilityKind.DIRECT,
    permissions=("missions.read",), data_classes=(DataClass.INTERNAL,))
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
    CapabilityKind.DIRECT, permissions=("missions.read",), data_classes=(DataClass.INTERNAL,))
SOURCES_SEARCH = CapabilityManifest(
    "sources.search", "Search connected knowledge sources; returns references, not copies.",
    CapabilityKind.DIRECT, permissions=("sources.read",),
    input_schema={"type": "object",
                  "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                  "required": ["query"]},
    data_classes=(DataClass.INTERNAL,))
CRM_LOOKUP = CapabilityManifest(
    "crm.lookup_account", "Look up a CRM account/contact by name, email, or id.",
    CapabilityKind.DIRECT, permissions=("crm.read",), input_schema=_obj_schema(query="string"),
    data_classes=(DataClass.CUSTOMER_CONTENT, DataClass.PII))


def _ok(output: Any, classes) -> HandlerResult:
    return HandlerResult(ok=True, output=output, data_classes=classes)


def build_read_registry(*, projections: Optional[ProjectionsReadPort] = None,
                        missions: Optional[MissionReadPort] = None,
                        sources: Optional[SourcesReadPort] = None,
                        crm: Optional[CrmReadPort] = None,
                        registry: Optional[CapabilityRegistry] = None) -> CapabilityRegistry:
    """Register the read capabilities whose backend is provided, onto a new or given registry."""
    reg = registry or CapabilityRegistry()

    if projections is not None:
        reg.register(PROJECTS_LIST,
                     lambda req, env: _ok(projections.projects(), (DataClass.INTERNAL,)))
        reg.register(PROJECTS_GET_STATUS,
                     lambda req, env: _ok(projections.overview(str(req.arguments.get("project_id", ""))),
                                          (DataClass.INTERNAL,)))
    if missions is not None:
        reg.register(MISSIONS_LIST,
                     lambda req, env: _ok(missions.list(), (DataClass.INTERNAL,)))
        reg.register(MISSIONS_GET,
                     lambda req, env: _ok(missions.get(str(req.arguments.get("mission_id", ""))),
                                          (DataClass.INTERNAL,)))
        reg.register(MISSIONS_EXPLAIN,
                     lambda req, env: _ok(missions.explain(str(req.arguments.get("mission_id", ""))),
                                          (DataClass.INTERNAL,)))
        reg.register(MISSIONS_PENDING,
                     lambda req, env: _ok(missions.pending_approvals(), (DataClass.INTERNAL,)))
    if sources is not None:
        reg.register(SOURCES_SEARCH,
                     lambda req, env: _ok(sources.search(str(req.arguments.get("query", "")),
                                                         int(req.arguments.get("limit", 10))),
                                          (DataClass.INTERNAL,)))
    if crm is not None:
        reg.register(CRM_LOOKUP,
                     lambda req, env: _ok(crm.lookup(str(req.arguments.get("query", ""))),
                                          (DataClass.CUSTOMER_CONTENT, DataClass.PII)))
    return reg


READ_CAPABILITIES = (
    PROJECTS_LIST, PROJECTS_GET_STATUS, MISSIONS_LIST, MISSIONS_GET, MISSIONS_EXPLAIN,
    MISSIONS_PENDING, SOURCES_SEARCH, CRM_LOOKUP)


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
