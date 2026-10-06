"""Capability coverage mapping (Phase 4, plan §12).

Map a WorkflowPain against what ReDevOps already has — connectors, runtime capabilities and packaged workflow
templates — and classify the gap. The crucial, non-obvious output: discovery can find that an existing
capability simply has not been PACKAGED (``PACKAGE_AS_TEMPLATE``) — the engine is not only a feature generator.

Provider-neutral: a ``CapabilityCatalog`` is federated from whatever inventories the caller has (the enterprise
side federates the mission CapabilityRegistry, app profiles, integration CAPABILITIES and the workflow-pack
catalog into it). ``default_catalog()`` is seeded from the connectors + obligation/market workflow packs this
stack actually ships, so the map reflects real coverage out of the box.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, List, Sequence, Tuple

from .contracts import WorkflowPain

CLASSIFICATION = ("ALREADY_SUPPORTED", "PACKAGE_AS_TEMPLATE", "SMALL_INTEGRATION_GAP", "NEW_CONNECTOR_REQUIRED",
                  "NEW_DOMAIN_LOGIC_REQUIRED", "NEW_RUNTIME_PRIMITIVE_REQUIRED", "OUT_OF_SCOPE")


@dataclass(frozen=True)
class WorkflowTemplate:
    name: str
    apps: FrozenSet[str]                 # a template covers a pain whose apps are a subset of these


@dataclass(frozen=True)
class CapabilityCatalog:
    connectors: FrozenSet[str] = frozenset()      # app names we have a connector for
    templates: Tuple[WorkflowTemplate, ...] = ()  # packaged workflow templates
    runtime_capabilities: FrozenSet[str] = frozenset()

    def covering_template(self, apps: FrozenSet[str]) -> str:
        for t in self.templates:
            if apps and apps <= t.apps:
                return t.name
        return ""


@dataclass(frozen=True)
class CapabilityCoverage:
    workflow_pain_id: str
    existing_connectors: Tuple[str, ...]
    missing_connectors: Tuple[str, ...]
    existing_workflow_templates: Tuple[str, ...]
    coverage_percent: float
    classification: str


def map_coverage(pain: WorkflowPain, catalog: CapabilityCatalog) -> CapabilityCoverage:
    """Classify a WorkflowPain against the catalog. Connector coverage + whether a packaged template already
    covers the app set decides ALREADY_SUPPORTED vs PACKAGE_AS_TEMPLATE vs a connector gap."""
    apps = frozenset(pain.applications)
    pid = pain.name or pain.digest()
    if not apps:
        return CapabilityCoverage(pid, (), (), (), 0.0, "OUT_OF_SCOPE")

    supported = tuple(sorted(a for a in apps if a in catalog.connectors))
    missing = tuple(sorted(a for a in apps if a not in catalog.connectors))
    template = catalog.covering_template(apps)
    coverage = round(len(supported) / len(apps), 4)

    if len(missing) == len(apps):
        classification = "NEW_CONNECTOR_REQUIRED"
    elif missing:
        classification = "SMALL_INTEGRATION_GAP" if len(missing) == 1 else "NEW_CONNECTOR_REQUIRED"
    elif template:
        classification = "ALREADY_SUPPORTED"
    else:
        # every connector exists but no packaged workflow — the "not marketed/packaged" discovery
        classification = "PACKAGE_AS_TEMPLATE"

    # a cross-app pain that needs a decision/exception (high agentic fit) but has no template may need new
    # domain logic rather than just packaging — surface that distinctly when connectors are all present.
    if classification == "PACKAGE_AS_TEMPLATE" and pain.agentic_fit == "high":
        classification = "NEW_DOMAIN_LOGIC_REQUIRED"

    return CapabilityCoverage(
        workflow_pain_id=pid, existing_connectors=supported, missing_connectors=missing,
        existing_workflow_templates=((template,) if template else ()), coverage_percent=coverage,
        classification=classification)


def default_catalog() -> CapabilityCatalog:
    """Seeded from the connectors + obligation/market workflow packs this stack actually ships."""
    connectors = frozenset({
        "stripe", "quickbooks", "netsuite", "xero", "salesforce", "hubspot", "zendesk", "jira", "servicenow",
        "okta", "erpnext", "slack", "microsoft teams", "teams", "whatsapp", "shopify",
    })
    templates = (
        WorkflowTemplate("payout-reconciliation", frozenset({"stripe", "quickbooks", "netsuite", "xero"})),
        WorkflowTemplate("support-360", frozenset({"salesforce", "hubspot", "zendesk", "jira", "servicenow", "stripe"})),
        WorkflowTemplate("approval-gated-action", frozenset({"slack", "teams", "microsoft teams", "erpnext"})),
        WorkflowTemplate("field-sync", frozenset({"salesforce", "hubspot", "zendesk", "okta", "erpnext"})),
    )
    return CapabilityCatalog(connectors=connectors, templates=templates)
