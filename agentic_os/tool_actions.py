"""The grant action each in-repo agent tool requires, as TRUSTED registration metadata.

``permissions.make_authorizer(..., tool_actions=...)`` authorises a tool at the action registered here —
never at one inferred from the tool's name, its description or an MCP ``readOnlyHint`` — and denies a tool that
has no entry. This table is written and reviewed by the code that registers the tools; a tool's own
self-description cannot change it. ``tests/test_tool_actions_complete.py`` fails when an app registers a tool
that is missing here, when an entry no longer exists, or when a tool registered ``side_effecting=True`` is
classified ``read``.

How a tool is classified (by what it ACTUALLY does, read from its launcher, not by its flag or its name):

* ``read`` — returns existing state. Any process it spawns has a FIXED argv with a read-only subcommand (no
  caller-controlled argument); any network call is a query to a platform service; it writes no files and starts no
  work; an in-memory cache is not state.
* ``write`` — changes state anywhere, or INITIATES work: launches a scanner or job against a caller-influenced
  target, writes files (a scanner's cache), downloads data or pulls images from an external registry, or
  directs another service to scan. A tool whose effects cannot be established is NOT classified (so it is
  denied) until someone decides. There is no weaker-than-write grade for "runs a process" because the plane has
  no ``execute`` action: see ``docs/PERMISSIONS_MIGRATION.md``.

The four tools registered ``side_effecting=True`` require ``write``, and so do the six scanner launchers below
(their ``side_effecting`` flag is false, but reading the launchers shows they start processes and write):
``supply_chain_scan`` (``trivy fs|rootfs`` on a caller-supplied path: DB download and cache writes),
``list_vulnerabilities`` (starts that same scan when no result is cached), ``scan_app`` / ``scan_apps``
(``docker inspect`` then ``trivy image``: pulls images from registries), ``sbom`` (``syft`` or ``trivy fs
--format cyclonedx``: process launch, possible DB download and cache writes) and ``malware_scan`` (directs the ClamAV
daemon to scan a caller-chosen path). ``list_scannable_apps`` (``docker ps``: fixed argv, listing only),
``scanner_status`` (``shutil.which``), ``explain_vulnerability`` (in-memory cache), ``scan_status`` and
``scan_results`` (query a job in the deploy-scan service) stay ``read``.
The ``books``, ``billing`` and ``compliance`` consoles register their tools with ``assistant.Tool`` (not ``_crtool``): the same
classification applies, and ``Assistant(authorizer=...)`` consults the same gate. ``chase_overdue`` and ``refund`` stage money-moving
actions (``side_effecting=True``: ``write``); ``run_scan`` returns the latest results (a fresh scan is run by the operator elsewhere).
``deploy/sidekick-devops/mcp_server.py`` registers its tool through the MCP SDK, which is not the ToolRegistry
authorizer seam, and is not covered here: it is gated at its own transport instead (stdio by default; an http listener needs a bearer token and
refuses to start without one; the ambient AWS credentials are used only with SIDEKICK_MCP_ALLOW_AWS=1).
"""
from __future__ import annotations

TOOL_ACTIONS: dict[str, dict[str, str]] = {
    "books": {
        "financials_summary": "read",
        "list_unpaid_invoices": "read",
        "find_party": "read",
        "close_status": "read",
    },
    "billing": {
        "billing_summary": "read",
        "list_overdue": "read",
        "find_customer": "read",
        "chase_overdue": "write",
        "refund": "write",
    },
    "compliance": {
        "scan_summary": "read",
        "list_failures": "read",
        "explain_finding": "read",
        "run_scan": "read",
    },
    "agentic-privacy": {
        "privacy_summary": "read",
        "list_requests": "read",
        "data_map": "read",
    },
    "edge-sentinel": {
        "threat_summary": "read",
        "list_bans": "read",
        "explain_alert": "read",
        "ban_ip": "write",
        "supply_chain_scan": "write",
        "list_vulnerabilities": "write",
        "explain_vulnerability": "read",
        "scan_app": "write",
        "scan_apps": "write",
        "list_scannable_apps": "read",
        "vuln_db_lookup": "read",
        "vuln_db_permissions": "read",
        "sbom": "write",
        "scanner_status": "read",
        "scan_deployment": "write",
        "scan_status": "read",
        "scan_results": "read",
        "malware_scan": "write",
        "wazuh_status": "read",
        "network_access_review": "read",
        "network_posture": "read",
        "propose_network_policy": "write",
        "backup_status": "read",
    },
    "growth-engine": {
        "traffic_summary": "read",
        "top_pages": "read",
        "top_referrers": "read",
    },
    "market-radar": {
        "watch_summary": "read",
        "recent_changes": "read",
        "list_watches": "read",
        "add_watch": "write",
    },
    # ── the apps whose consequential tools are executed through their routes and operators (basic permissions without the enterprise package).
    # Classified from what the code DOES (per-tool evidence: docs/PERMISSIONS_MIGRATION.md section 15); the enterprise fleet table is cross-checked,
    # never imported or copied. Anything not listed here is unclassified and DENIED.
    "control-tower": {"bi.summary": "read", "bi.ask": "read", "bi.refresh": "read"},
    "document-editor": {
        "document.connect_check": "read", "document.inspect": "read", "document.open_session": "read", "document.propose_patch": "read",
        "document.convert": "write", "document.export": "write", "document.apply_patch": "write", "document.set_label": "write",
        "document.publish": "write",
    },
    "growth-assistant": {
        "assistant.ask": "read", "assistant.playbook": "write", "assistant.subreddit_plan": "write", "assistant.founder_content": "write",
        "assistant.community_blueprint": "write", "assistant.cold_outreach": "write", "assistant.hire_brief": "write",
        "assistant.push_assets": "write",
    },
    "guide": {"guide.retrieve": "read", "guide.walkthrough": "read"},
    "infra": {
        "infra.verify": "read", "infra.plan": "write", "infra.drift": "write", "infra.provision": "write", "infra.configure": "write",
        "infra.rollback_release": "write", "infra.destroy_delta": "delete",
    },
    "outreach-engine": {
        "outreach.refresh": "read", "outreach.pipeline_summary": "read", "outreach.draft_followup": "read", "outreach.synced": "read",
        "outreach.discover": "write", "outreach.approve": "write", "outreach.send_all": "write",
    },
    "social-autopilot": {
        "social.activity": "read", "social.vibex_trends": "read", "social.draft": "write", "social.draft_visual": "write",
        "social.publish": "write", "social.record_reel": "write", "social.vibex_generate": "write",
    },
    "agentic-crm": {
        "pipeline_summary": "read", "stage_breakdown": "read", "lead_queue": "read", "connection_status": "read",
        "crm.score_lead": "write", "crm.research": "write", "crm.draft_outreach": "write", "crm.qualify": "write",
    },
    "lifecycle": {
        "list_campaigns": "read", "list_segments": "read", "lifecycle_summary": "read", "lifecycle.segment": "read",
        "lifecycle.suggest_flow": "read", "lifecycle.compose_campaign": "write",
    },
    "support": {
        "inbox_summary": "read", "list_conversations": "read", "draft_reply": "read", "support.draft_reply": "write",
        "support.resolve": "write", "support.escalate": "write", "support.send_onboarding": "write",
    },
}


# The Operator SDK capabilities of the apps classified above (the Mission Runtime's execution path; ``Operator._execute`` asks the gate for each).
# From what the code does; the registered ``side_effecting`` flag is the cross-check. One disagreement, reported: ``compliance.scan`` is registered
# non-side-effecting but runs the oscap scan script (a process launch that rewrites the results file), so it is ``write``, like the scanner
# launchers. Cancelling a subscription and erasing a data subject's data remove things: ``delete``.
_OPERATOR_CAPABILITIES: dict[str, dict[str, str]] = {
    "agentic-privacy": {"privacy.retention": "read", "privacy.intake": "write", "privacy.access": "write", "privacy.delete": "delete"},
    "billing": {"billing.summary": "read", "billing.dunning": "write", "billing.refund": "write", "billing.create_subscription": "write",
                "billing.cancel_subscription": "delete"},
    "books": {"books.reconcile": "read", "books.categorize": "write", "books.close": "write", "books.record_revenue": "write",
              "books.reverse_entry": "write"},
    "compliance": {"compliance.explain": "read", "compliance.scan": "write", "compliance.remediate": "write", "compliance.file_consent": "write"},
    "edge-sentinel": {"sentinel.triage": "read", "sentinel.block_ip": "write", "sentinel.unblock_ip": "write"},
    "growth-engine": {"growth.analyze": "read", "growth.reallocate_budget": "write"},
    "market-radar": {"radar.brief": "read", "radar.add_watch": "write"},
}
for _app, _caps in _OPERATOR_CAPABILITIES.items():
    TOOL_ACTIONS[_app].update(_caps)


# Tools that touch no controlled data and are candidates for ``make_authorizer(public_tools=...)``. A public tool is NOT exempt from
# classification: it is listed in TOOL_ACTIONS too, as ``read`` (the gate denies an unlisted public tool, and refuses to build when a
# public tool is registered write or delete). (app, tool) pairs.
PUBLIC_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("agentic-privacy", "data_map"),
    ("edge-sentinel", "list_scannable_apps"),
    ("edge-sentinel", "vuln_db_lookup"),
    ("edge-sentinel", "vuln_db_permissions"),
    ("edge-sentinel", "scanner_status"),
)


def tool_actions(app: str) -> dict[str, str]:
    """The ``tool_actions`` mapping for ``make_authorizer`` for one app (empty when the app has no tools)."""
    return dict(TOOL_ACTIONS.get(app, {}))


class ToolActionsConflict(ValueError):
    """The kernel's table and the enterprise package's table classify the same tool of one app differently."""


# PUBLIC INTAKE: a distinct classification, separate from read / write grants. It belongs to ROUTES registered here by id (app, method, path), never to a tool or an operator name, and it names the ONE tool
# whose action the route may perform on behalf of an unauthenticated data subject: open a PENDING request. The tool itself stays classified ``write`` above, so every ordinary interface (the Operator,
# ``/invoke``, the console) still needs the write grant. The registry is trusted code (no environment variable or configuration adds to it); ``app_gate.public_intake`` mints the ticket the one
# core function that creates the pending request insists on.
import types as _types

PUBLIC_INTAKE_ROUTES = _types.MappingProxyType({
    "agentic-privacy": _types.MappingProxyType({("POST", "/intake"): "privacy.intake", ("POST", "/request"): "privacy.intake"}),
})


def _enterprise_package_trusted() -> bool:
    """Is ``context_runtime_enterprise`` provided by an INSTALLED distribution? ``sys.path`` can hold a plain directory or file with that name (a planted module executes on
    import, and could classify tools as it likes): only the package an installed distribution's metadata lists is trusted (see ``trusted_import``); nothing is executed to find out."""
    from . import trusted_import
    return trusted_import.package_status("context_runtime_enterprise") == "trusted"       # DR-02: the located __init__ must be a file the distribution lists


def _enterprise_table(app: str):
    """The enterprise package's classification of ``app`` (``context_runtime_enterprise.fleet_tool_actions``), or None when the package is not an installed distribution, the
    table is absent, or it does not classify the app. Never imported from an untrusted location."""
    if not _enterprise_package_trusted():
        return None
    try:
        from context_runtime_enterprise.fleet_tool_actions import fleet_tool_actions
    except ImportError:
        return None
    try:
        return dict(fleet_tool_actions(app))
    except Exception:  # noqa: BLE001 — "classified in the kernel" / "unknown app" both mean: no enterprise table for it
        return None


def resolve_tool_actions(app: str, *, enterprise: bool | None = None) -> dict[str, str] | None:
    """The ONE classified table an app passes to its permissions bootstrap: the kernel's. Anything the kernel does not list is unclassified and DENIED.

    ``enterprise=True`` (the enterprise mode only; basic permissions never import the package) cross-checks the enterprise package's table, and ONLY if an installed distribution
    provides it (FR-11). Its entries can only ADD DENIALS, never allowances: a tool the kernel does not list stays unclassified (an entry for it is ignored, so a table from elsewhere cannot
    make a new tool authorisable), and a tool both list with different actions raises ``ToolActionsConflict`` (the app refuses to start). None when the kernel does not classify the app."""
    kernel = TOOL_ACTIONS.get(app)
    if kernel is None:
        return None
    merged = dict(kernel)
    if enterprise is None or enterprise:          # the default cross-checks (when an installed distribution provides the package); basic mode passes False
        for tool, action in (_enterprise_table(app) or {}).items():
            if tool in merged and merged[tool] != action:
                raise ToolActionsConflict(f"app {app!r}, tool {tool!r}: the kernel table says {merged[tool]!r}, the enterprise table says {action!r}")
    return merged
