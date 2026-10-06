"""Product Opportunity Intelligence Phase 4 — capability coverage mapping.

Proves: a pain fully covered by a shipped template → ALREADY_SUPPORTED; connectors present but no template →
PACKAGE_AS_TEMPLATE (or NEW_DOMAIN_LOGIC_REQUIRED when it needs a decision); one missing connector → SMALL_
INTEGRATION_GAP; all missing → NEW_CONNECTOR_REQUIRED; no apps → OUT_OF_SCOPE. The default catalog reflects the
connectors + packs this stack ships.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.product_opportunity import WorkflowPain, default_catalog, map_coverage


def _wp(apps, *, name="wp", agentic_fit="medium"):
    return WorkflowPain(prov=Provenance(provider="t"), name=name, applications=tuple(apps), agentic_fit=agentic_fit)


def test_already_supported_when_template_covers():
    cov = map_coverage(_wp(["stripe", "quickbooks"]), default_catalog())
    assert cov.classification == "ALREADY_SUPPORTED" and cov.coverage_percent == 1.0
    assert cov.existing_workflow_templates == ("payout-reconciliation",)


def test_package_as_template_when_connectors_but_no_template():
    # both connectors exist, but no shipped template covers this exact pair, and it's not a high-agentic decision
    cov = map_coverage(_wp(["slack", "jira"], agentic_fit="low"), default_catalog())
    assert cov.classification == "PACKAGE_AS_TEMPLATE" and not cov.missing_connectors


def test_new_domain_logic_when_agentic_and_unpackaged():
    cov = map_coverage(_wp(["shopify", "netsuite"], agentic_fit="high"), default_catalog())
    assert not cov.missing_connectors and cov.classification == "NEW_DOMAIN_LOGIC_REQUIRED"


def test_small_integration_gap_one_missing():
    cov = map_coverage(_wp(["stripe", "chargebee"]), default_catalog())   # chargebee not a connector
    assert cov.classification == "SMALL_INTEGRATION_GAP" and cov.missing_connectors == ("chargebee",)
    assert cov.existing_connectors == ("stripe",) and cov.coverage_percent == 0.5


def test_new_connector_required_all_missing():
    cov = map_coverage(_wp(["magento", "sage"]), default_catalog())
    assert cov.classification == "NEW_CONNECTOR_REQUIRED" and cov.coverage_percent == 0.0


def test_out_of_scope_no_apps():
    assert map_coverage(_wp([]), default_catalog()).classification == "OUT_OF_SCOPE"
