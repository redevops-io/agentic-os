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

This table is not only what the gate reads — it is what the registrations read. ``console_tool(app)`` returns the
app's console-tool constructor with each tool's class bound from here, so a registration site never names a class
of its own, and ``mount_actions(app, ...)`` does the same for an MCP mount. An app that passed hand-written
classes at the call site would hold a second copy of this classification, and the copy is the one that drifts.
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
        # The mounted MCP tool (``AgentConsole.mount_mcp``, no prefix → the server's own name). Its server
        # (``context_runtime.tools.mcp_servers.web_search``) makes two anonymous HTTPS GETs — the Wikipedia REST
        # search and the Hacker News Algolia search — and returns titles and URLs: no file, no job, no state.
        # The server's ``readOnlyHint`` is why the approval policy lets it run; this row is why the plane lets it
        # run, and the mount reads it from here (``mount_actions``) rather than trusting the announcement.
        "web_search": "read",
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
        "outreach.enrich": "read", "outreach.draft_pitch": "read", "outreach.check_replies": "read",
        "outreach.follow_up": "read",
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
    "growth-engine": {"growth.analyze": "read", "growth.remeasure": "read", "growth.reallocate_budget": "write"},
    # the loop on the company's own site reads public pages and writes nothing; the approval is a person's decision and is held to "write"
    "market-radar": {"radar.brief": "read", "radar.add_watch": "write", "radar.synthetic_study": "write", "radar.site_audit": "read",
                     "radar.recommend": "read", "radar.approve_plan": "write", "radar.verify_change": "read", "radar.reassess": "read"},   # writes nothing, but spends model calls: held to the stricter action
}
for _app, _caps in _OPERATOR_CAPABILITIES.items():
    TOOL_ACTIONS[_app].update(_caps)


# Tools that touch no controlled data and are candidates for ``make_authorizer(public_tools=...)``. A public tool is NOT exempt from
# classification: it is listed in TOOL_ACTIONS too, as ``read`` (the gate denies an unlisted public tool, and refuses to build when a
# public tool is registered write or delete). (app, tool) pairs.
#: The kernel's governed-writes plane (agentic_os.governed_writes): what external agents do with ERPNext, support and CRM.
#: A lookup or following one's own request reads; a proposal is a write (it becomes a change once a person approves it).
TOOL_ACTIONS["governed-writes"] = {
    "erp.find_records": "read", "erp.get_record": "read", "erp.request_status": "read",
    "support.list_conversations": "read", "support.get_conversation": "read", "support.request_status": "read",
    "crm.find_companies": "read", "crm.find_people": "read", "crm.request_status": "read",
    "erp.request_comment": "write", "erp.request_draft_purchase_order": "write", "support.request_private_note": "write",
    "crm.request_company": "write", "crm.request_person": "write",
    "economics.discrepancies": "read", "economics.explain_adjustment": "read", "economics.evidence": "read", "economics.findings": "read", "economics.bank_reconciliation": "read",
    "economics.request_status": "read", "economics.request_payment_correction": "write",
    "media.library": "read", "media.assets": "read", "media.inspect": "read", "media.import": "write", "media.compose": "write",
    "media.export": "write", "media.request_generate": "write", "media.request_status": "read",
    "social.channels": "read", "social.posts": "read", "social.request_post": "write", "social.request_status": "read",
    "market.sites": "read", "market.monitor": "read", "market.research": "write", "market.list_research": "read",
    "market.get_research": "read", "market.evidence": "read", "market.request_watch": "write", "market.request_change_plan": "write",
    "market.verify_plan": "read", "market.request_status": "read",
    "apps.list": "read", "apps.get": "read", "apps.state.list": "read", "apps.state.add": "write", "apps.state.update": "write",
    "apps.request_install": "write", "apps.request_status": "read",
}

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


class UnclassifiedTool(ValueError):
    """An app tried to register a tool this table does not classify. Not a warning: an unclassified tool is
    denied at every call, so an app that ships one has shipped a capability nobody can use. It is refused at
    import, where the developer sees it, rather than at the console, where the user does not."""


class RegistrationConflict(ValueError):
    """What a registration says about itself contradicts what this table says about it. Both spellings of the
    registration (the risk class the call passes and the ``side_effecting`` approval flag) are checked, because
    a table a registration can quietly disagree with is a table nobody can rely on."""


def action_for(app: str, tool: str) -> str:
    """The reviewed risk class of one bundled tool — the ONLY place an app reads its tools' classes from.

    An app that hands ``tool_actions`` to its gate and then passes a hand-written class at each registration
    has two tables and one of them is a guess. Every console registration in this repository goes through here
    instead (``console_tool``), so a tool's class exists in exactly one reviewed place.

    Raises :class:`UnclassifiedTool` when the app or the tool is not listed: the alternative is a default, and
    a default is how an unread capability ends up classified as the cheapest thing to grant."""
    try:
        action = TOOL_ACTIONS[app][tool]
    except KeyError:
        raise UnclassifiedTool(
            f"tool '{app}.{tool}' has no entry in agentic_os/tool_actions.py. Read what the tool ACTUALLY does "
            f"(its launcher, not its name) and add it as read, write or delete; an unclassified tool is denied "
            f"every call, so registering one would ship a capability nobody can use.") from None
    return action


def console_tool(app: str):
    """The app's console-tool constructor: ``context_runtime``'s ``tool()`` with the class bound from THIS table.

    An app replaces its import of the console's ``tool`` with::

        from agentic_os.tool_actions import console_tool
        _crtool = console_tool(_GATE_APP)

    and every ``_crtool("traffic_summary", "...", fn)`` then carries the reviewed class without the call site
    naming one — which is the point: the risk class of a tool is decided once, in the reviewed table, by
    whoever read what the tool does. Three things are refused here, at import, rather than left to deny a user
    later: a tool the table does not classify (:class:`UnclassifiedTool`); a call that passes its own ``action``
    that disagrees with the table, which would be a second table; and a tool registered ``side_effecting=True``
    but classified ``read``, which is the approval flag contradicting the authorization class (the harness
    refuses that combination too — asking for the weakest grant the plane issues for a tool that changes
    things is always a mistake, never a shortcut).

    ``side_effecting`` stays the APPROVAL axis and is passed through untouched: authorization and approval are
    two checks, and a class here never grants or approves anything by itself."""
    def register(name: str, description: str, fn, *, parameters: dict | None = None,
                 side_effecting: bool = False, action: str | None = None):
        declared = action_for(app, name)
        if action is not None and action != declared:
            raise RegistrationConflict(
                f"tool '{app}.{name}' is registered with action={action!r} but it is classified {declared!r} in "
                f"agentic_os/tool_actions.py. Drop the argument and let the table decide — one tool, one reviewed "
                f"class — or change the table with the evidence for what the tool does.")
        if side_effecting and declared == "read":
            raise RegistrationConflict(
                f"tool '{app}.{name}' is registered side_effecting=True but classified read in "
                f"agentic_os/tool_actions.py. A read grant cannot authorize a tool that changes state or starts "
                f"work: classify it write (or delete), or drop the flag if it really only reads.")
        from context_runtime.integrations.agent_console import tool as _console_tool     # late: the base install has no context-runtime
        return _console_tool(name, description, fn, parameters=parameters,
                             side_effecting=side_effecting, action=declared)

    register.app = app
    return register


def mount_actions(app: str, *tools: str) -> dict[str, str]:
    """The ``actions`` map for ``AgentConsole.mount_mcp(...)``, read from this table.

    A mounted MCP server may announce ``readOnlyHint``; that is an approval hint and never an authorization
    class, because the thing asking to be run is not allowed to grade its own risk. Whoever mounts the server
    declares its classes — and here that declaration lives in the same reviewed table as every in-repo tool,
    keyed by the registered name, so the mount cannot drift from it. A tool not listed is refused rather than
    defaulted: ``mount_mcp`` would give an unlisted tool its ``default_action`` (write, the strictest), which
    is safe but silent, and a silent default in a classification table is how a table stops being true."""
    return {tool: action_for(app, tool) for tool in tools}


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
