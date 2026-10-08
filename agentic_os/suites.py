"""The six domain suites — the enterprise "domain agents" as compositions over the canonical apps.

Each suite is the runtime-native expression of what used to be a separate v6 domain-agent service
(the ``absorbs`` mapping in the deploy compose). The suite composes the canonical public apps; the
enterprise-only intelligence (Decision Flow Planner, discovery, learning bindings) attaches at
runtime via the overlay seams, not here. These definitions do not replace the live services yet —
they are the canonical declaration the services can be generated from.
"""
from __future__ import annotations

from agentic_os.app_kit.manifest import EnterpriseRequirement
from agentic_os.app_kit.suite import Suite

REVENUE = Suite(
    name="revenue",
    description="Lead-gen, outreach and pipeline across CRM, outreach, market signals and founder content.",
    apps=("agentic-crm", "outreach-engine", "market-radar", "growth-assistant"),
    mission_templates=("revenue_rescue", "inbound_lead"),
)

INTELLIGENCE = Suite(
    name="intelligence",
    description="Business visibility + discovery: analytics, attribution and competitor watches.",
    apps=("control-tower", "growth-engine", "market-radar"),
)

FINANCE = Suite(
    name="finance",
    description="Continuous financial operations: billing/dunning/refunds and bookkeeping/close.",
    apps=("billing", "books"),
    mission_templates=("invoice_recovery",),
)

CUSTOMER_SUCCESS = Suite(
    name="customer-success",
    description="Discovery-driven customer missions: support, in-product help and lifecycle marketing.",
    apps=("support", "guide", "lifecycle"),
    mission_templates=("onboarding",),
)

CONTENT = Suite(
    name="content",
    description="Founder/brand content across social publishing and growth advisory.",
    apps=("social-autopilot", "growth-assistant"),
    mission_templates=("content_distribution",),
)

SECURITY_COMPLIANCE = Suite(
    name="security-compliance",
    description="The multi-representation investigations platform: compliance, privacy (DSAR) and edge security.",
    apps=("compliance", "agentic-privacy", "edge-sentinel"),
    enterprise=EnterpriseRequirement.OPTIONAL,
)

#: all six, in the order the catalog presents the domains.
DOMAIN_SUITES = (REVENUE, INTELLIGENCE, FINANCE, CUSTOMER_SUCCESS, CONTENT, SECURITY_COMPLIANCE)


def register_domain_suites(app_registry):
    """Register the six domain suites against a populated AppRegistry, returning the SuiteRegistry."""
    from agentic_os.app_kit.suite import SuiteRegistry

    reg = SuiteRegistry(app_registry)
    for suite in DOMAIN_SUITES:
        reg.register(suite)
    return reg


__all__ = [
    "REVENUE", "INTELLIGENCE", "FINANCE", "CUSTOMER_SUCCESS", "CONTENT", "SECURITY_COMPLIANCE",
    "DOMAIN_SUITES", "register_domain_suites",
]
